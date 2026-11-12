"""PIMiner conversations and rolling memory with an audited fixed route/P0.

The saved initial analysis is visible in the first revision prompt. Historical
provider hidden state and historical target results are never reconstructed.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import tempfile
import uuid

from .experience import HISTORY_FIELDS, RollingExperience
from .initialization import InitializationBundle
from .piminer import GenerationError, PIMinerAdapter, PUBLIC_FIELDS, REVISION_PROTOCOL
from .session import PersistentRoleDecision
from .revision import audit_revision, revision_prompt, revision_role
from ..schema import Payload
from ..evidence.ledger import EvidenceLedger, validate_goal_spec
from ..evidence.observer import policy_for_case
from ..vendor import router
from ..vendor import attacker_observer as native_observer
from ..vendor.delta import build_delta_block
from ..vendor.prompts import _format_attempt, build_user_message_blocks


class PIMinerAlignedAdapter:
    use_cached_p0 = False
    mode = 'piminer_aligned'

    def __init__(self, root, model, target_model, initialization_manifest,
                 effort='max', timeout=900, session_factory=None, revision_profile='original',
                 a2_library=None, a2_retrieval=None, false_revision_mode='original',
                 evidence_decide=None, rolling_experience=True):
        self.root = Path(root).resolve()
        if type(rolling_experience) is not bool:
            raise ValueError('rolling_experience must be boolean')
        self.rolling_experience = rolling_experience
        self.target_model = target_model
        from .transport import RoleDecision
        from ..feedback.assessment import SYSTEM
        self.autonomous_decide = evidence_decide or RoleDecision(model, effort, timeout, SYSTEM)
        self.bundle = InitializationBundle(self.root, initialization_manifest)
        self.role = (self.root / 'src/vendor/attacker_role.txt').read_text()
        self.revision_profile = revision_profile
        self.revision_role = revision_role(self.role, revision_profile)
        if false_revision_mode not in ('original', 'grounded'):
            raise ValueError('Unknown False revision mode')
        self.false_revision_mode = false_revision_mode
        self.false_attacker = (PIMinerAdapter(self.root, model, effort, timeout,
                                             decide=evidence_decide)
                               if false_revision_mode == 'grounded' else None)
        if (a2_library is None) != (a2_retrieval is None):
            raise ValueError('a2_library and a2_retrieval must be supplied together')
        self.resources = None
        if a2_library is not None:
            from .a2 import A2Resources
            self.resources = A2Resources(self.root, a2_library, a2_retrieval)
        self.session_factory = session_factory or PersistentRoleDecision
        self.session_options = dict(model=model, effort=effort, timeout=timeout,
                                    system_prompt=self.revision_role)
        self.routes, self.sessions, self.seen_history = {}, {}, {}
        # All generation paths share one immutable history prefix. The U
        # conversation separately tracks only the feedback it has already seen.
        self.generation_history = {}
        self.grounded_evidence_history = {}
        self.grounded_plan_history = {}
        self.memories, self.active_branch = {}, None
        self.run_path = None

    def bind_run(self, run_path, policy_ids):
        if self.run_path is not None:
            raise ValueError('Adapter already belongs to a run')
        run_path = Path(run_path).resolve()
        manifest = json.loads((run_path / 'manifest.json').read_text())
        parent = manifest.get('parent_run')
        run_root = Path(parent).resolve() if parent else run_path
        if parent and run_root != run_path.parent:
            raise ValueError('Parallel worker must belong to its immediate parent run')
        policies = list(policy_ids)
        if not policies or any(not re.fullmatch(r'[A-Za-z0-9_-]+', p) for p in policies):
            raise ValueError('Invalid policy directory identity')
        self.run_path = run_path
        self.memories = {p: RollingExperience(run_root / 'experience' / p,
                                             enabled=self.rolling_experience) for p in policies}

    def begin_branch(self, case, policy_id):
        if policy_id not in self.memories:
            raise ValueError('Bind run and policy before revising')
        self.bundle.for_case(case)
        self.active_branch = (case['case_id'], policy_id)

    def finish_branch(self, case, policy_id, history, outcome):
        if self.active_branch != (case['case_id'], policy_id):
            raise ValueError('Completed branch identity differs')
        route = self.routes[case['case_id']]
        published = self.memories[policy_id].publish(case, history, outcome, route['strategy_ids'])
        self.active_branch = None
        return dict(published=published, path=str(self.memories[policy_id].path))

    def preflight(self):
        # No Router request is needed. Two neutral turns verify that the actual
        # provider/CLI preserves conversation state across separate processes.
        nonce = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix='piminer-session-preflight-') as directory:
            options = dict(self.session_options, system_prompt='Follow the user instructions exactly.')
            session = self.session_factory(**options, session_dir=Path(directory) / 'session')
            session('Remember this token for our next turn: ' + nonce + '. Reply only OK.',
                    'preflight_attacker_start')
            output = session('Return only the token I asked you to remember in the previous turn.',
                             'preflight_attacker_resume')
            if output.strip() != nonce:
                raise RuntimeError('Persistent Attacker preflight did not retain conversation state')
            result = dict(status='passed', router=dict(status='fixed_initialization', calls=[]),
                          attacker=dict(status='passed', continuity_verified=True,
                                        calls=deepcopy(session.calls)))
        if self.false_attacker is not None:
            result['grounded'] = self.false_attacker.preflight()
        return result

    def generate(self, case, payload, episode, packets, revision_id):
        return self.generate_with_history(case, payload, episode, packets, revision_id, ())

    def assess_and_decide(self, attempt, history, budget, *, feedback_view='final_only'):
        from ..feedback.assessment import assess
        return assess(attempt, history, budget, self.autonomous_decide, feedback_view=feedback_view)

    def revise(self, attempt, history, decision, *, feedback_view='final_only'):
        if self.false_attacker is not None:
            return self.false_attacker.revise(attempt, history, decision, feedback_view=feedback_view)
        from ..feedback.methods import revise_original
        return revise_original(attempt, history, decision, self.autonomous_decide, feedback_view=feedback_view)

    def generate_with_observed_history(self, case, payload, episode, packets, revision_id, history):
        return self.generate_with_history(case, payload, episode, packets, revision_id, history)

    @staticmethod
    def _false_packets(case, episode, packets):
        """Revalidate the scoped, current-rollout F before exposing observations."""
        if (episode.trigger != 'F' or episode.case_id != case['case_id']):
            raise ValueError('Evidence revision requires a current-case False episode')
        validate_goal_spec(case['goal_spec'])
        policy = policy_for_case(case)
        authorized = native_observer.definitions_for(policy)
        fingerprint = native_observer.fingerprint(policy)
        ledger = EvidenceLedger(episode.episode_id, case['goal_spec'])
        projected = []
        for packet in packets:
            if (packet.get('policy') != fingerprint
                    or packet.get('query_id') not in authorized):
                raise ValueError('Evidence packet exceeds current-case resource grants')
            if 'event_coverage' in packet:
                native_observer.validate_coverage_certificate(packet['event_coverage'], policy)
                if packet['event_coverage']['suite_name'] != case['suite_name']:
                    raise ValueError('Evidence coverage belongs to another suite')
            ledger.update(packet)
            # Planner input is restricted to the old public returned-observation
            # fields. Extra raw/evaluator fields on a packet are never forwarded.
            projected.append(dict(query_id=packet['query_id'],
                response={k: deepcopy(packet['response'][k]) for k in ('supported', 'evidence')}))
        if ledger.trigger != 'F':
            raise ValueError('Frozen authorized evidence does not establish False')
        return tuple(projected), ledger.records()

    def _compose_a2_strategy(self, case, initial, original_document):
        """Keep a bundle-verified cold-start member alongside normal A2 views."""
        ids = initial['strategy_ids']
        unknown = [sid for sid in ids if sid not in self.resources.strategies]
        if unknown and (unknown != ['_template_cold_start'] or ids.count('_template_cold_start') != 1):
            raise ValueError('Unknown fixed A2 strategy: ' + ', '.join(unknown))
        strategies = {}
        for identifier, snapshot in zip(ids, initial['strategy_paths']):
            if identifier == '_template_cold_start':
                # InitializationBundle.for_case has verified this fixed local
                # snapshot's identity, ordered route, and original byte hash.
                continue
            source = self.resources.strategies[identifier]['source']
            path = self.root / source
            if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256((self.root / snapshot).read_bytes()).digest():
                raise ValueError('Aligned A2 strategy source differs from fixed initialization: ' + identifier)
            strategies[identifier] = dict(id=identifier, path=str(path))
        if not unknown:
            return self.resources.compose(case, [strategies[sid] for sid in ids])
        task = self.resources.validate_case(case)
        docs, selections = [], {}
        for index, (identifier, snapshot) in enumerate(zip(ids, initial['strategy_paths']), 1):
            if identifier == '_template_cold_start':
                text = (self.root / snapshot).read_bytes().decode('utf-8')
            else:
                single, metadata = self.resources.compose(case, [strategies[identifier]])
                header = f'===== Candidate strategy 1/1: {identifier} =====\n\n'
                if not single.startswith(header):
                    raise ValueError('Unexpected A2 strategy document header')
                text = single[len(header):]
                selections.update(metadata['selections'])
            docs.append(f'===== Candidate strategy {index}/{len(ids)}: {identifier} =====\n\n{text}')
        document = '\n\n'.join(docs)
        metadata = dict(protocol=self.resources.protocol,
            library_sha256=self.resources.library_sha256,
            retrieval_sha256=self.resources.retrieval_sha256,
            public_sha256=task['public_sha256'], task_type=task['task_type'],
            selections=selections, template_fallbacks=['_template_cold_start'],
            reason='no_library_examples',
            strategy_document_sha256=hashlib.sha256(document.encode('utf-8')).hexdigest(),
            original_document_chars=len(original_document), a2_document_chars=len(document))
        if 'retrieval_method' in self.resources.retrieval:
            metadata['retrieval_method'] = deepcopy(self.resources.retrieval['retrieval_method'])
        return document, metadata

    def generate_with_history(self, case, payload, episode, packets, revision_id, history):
        record = dict(status='running', mode=self.mode, calls=[], candidates=[], checks=[])
        try:
            initial = self.bundle.for_case(case)
            if self.resources is not None:
                self.resources.validate_case(case)
            cid = case['case_id']
            if cid not in self.routes:
                if payload is not None:
                    raise ValueError('Load the fixed shared P0 before revising')
                route = dict(strategy_ids=initial['strategy_ids'],
                    strategy_paths=initial['strategy_paths'],
                    strategy_sha256={p: hashlib.sha256((self.root / p).read_bytes()).hexdigest()
                                     for p in initial['strategy_paths']},
                    document=self.bundle.strategy_document(case))
                if self.resources is not None:
                    document, metadata = self._compose_a2_strategy(case, initial, route['document'])
                    route.update(document=document, a2=metadata)
                self.routes[cid] = route
            route = self.routes[cid]
            record['routing'] = {k: deepcopy(v) for k, v in route.items() if k != 'document'}
            if payload is None:
                if history or revision_id != 'P0':
                    raise ValueError('Fixed initialization requires an empty P0 history')
                result = Payload('P0', initial['payload'])
                initialization = ('fresh_shared_route_and_p0' if initial['source_metadata'].get('kind') == 'fresh_public_generation'
                                  else 'historical_route_and_p0')
                record.update(status='ready', initialization=initialization,
                    revision_profile='original',
                    source_metadata=initial['source_metadata'], analysis=initial['analysis'],
                    payload=result.text, payload_sha256=result.sha256,
                    reused_route=True, reused_payload=True, reused_target_feedback=False)
                return result, record
            if (episode is None or episode.gold is not False or episode.error
                    or not history or history[-1]['injection'] != payload.text):
                raise ValueError('Revise requires a completed valid failure and its history')
            key = self.active_branch
            if key is None or key[0] != cid:
                raise ValueError('Revision must belong to the active case/policy branch')
            visible = [{k: deepcopy(h[k]) for k in HISTORY_FIELDS} for h in history]
            if (any(type(h['security']) is not bool or h['security'] for h in visible)
                    or any(type(h['iter']) is not int or h['iter'] != i for i, h in enumerate(visible))):
                raise ValueError('Revision history must contain consecutive completed failures')
            if (visible[0]['injection'] != initial['payload']
                    or visible[0]['analysis'] != initial['analysis']):
                raise ValueError('Branch history does not start with the fixed P0 and analysis')
            generated = self.generation_history.get(key, [])
            if visible[:len(generated)] != generated or len(visible) <= len(generated):
                raise ValueError('Revision history must extend the observed branch history')
            if self.false_attacker is not None and getattr(episode, 'trigger', None) == 'F':
                scoped_packets, current_predicates = self._false_packets(case, episode, packets)
                if not isinstance(visible[-1]['final_output'], str):
                    raise ValueError('Grounded revision requires projected black-box feedback')
                grounded_history = []
                previous_evidence = self.grounded_evidence_history.get(key, [])
                previous_plans = self.grounded_plan_history.get(key, [])
                for index, (original, attempt) in enumerate(zip(history, visible)):
                    item = deepcopy(attempt)
                    supplied_plan = original.get('revision_plan')
                    if index < len(previous_plans) and previous_plans[index] is not None:
                        if supplied_plan is not None and supplied_plan != previous_plans[index]:
                            raise ValueError('Previously consumed revision plan cannot change')
                        supplied_plan = previous_plans[index]
                    if supplied_plan is not None:
                        item['revision_plan'] = deepcopy(supplied_plan)
                    supplied_observation = original.get('revision_observation')
                    if index < len(previous_evidence) and previous_evidence[index] is not None:
                        if supplied_observation is not None and supplied_observation != previous_evidence[index]:
                            raise ValueError('Previously consumed grounded evidence cannot change')
                        supplied_observation = previous_evidence[index]
                    if supplied_observation is not None:
                        item['revision_observation'] = deepcopy(supplied_observation)
                    grounded_history.append(item)
                # Current F is revalidated from the frozen authorized packets.
                # Direct callers may lack Runner's richer history projection.
                current = dict(episode_id=episode.episode_id, case_id=cid,
                    policy_id=getattr(episode, 'policy_id', key[1]),
                    payload_sha256=payload.sha256, trigger='F', observation_error=False,
                    predicates=[{k: deepcopy(p[k]) for k in ('id', 'query', 'status', 'is_trigger')}
                                for p in current_predicates],
                    query_results=list(scoped_packets))
                supplied = grounded_history[-1].get('revision_observation')
                if supplied is not None and supplied != current:
                    raise ValueError('Latest history evidence differs from the current frozen episode')
                grounded_history[-1]['revision_observation'] = current
                record.update(revision_path='grounded_false', prompt_mode=REVISION_PROTOCOL,
                    revision_profile=self.revision_profile, analysis='',
                    history_iters=[h['iter'] for h in visible],
                    new_history_iters=[h['iter'] for h in visible[len(generated):]],
                    feedback_history_iters=[h['iter'] for h in visible],
                    evidence_episode_id=episode.episode_id,
                    evidence_queries=[p['query_id'] for p in scoped_packets])
                try:
                    result, grounded = self.false_attacker.generate(dict(case, policy_id=key[1]), payload, episode,
                        scoped_packets, revision_id, strategy_document=route['document'],
                        final_output=visible[-1]['final_output'], revision_history=grounded_history)
                except GenerationError as exc:
                    record.update(deepcopy(exc.record), analysis='')
                    raise
                record.update(grounded, analysis='')
                self.generation_history[key] = visible
                self.grounded_evidence_history[key] = [
                    deepcopy(h['revision_observation']) for h in grounded['revision_memory']['attempts']]
                self.grounded_plan_history[key] = [
                    deepcopy(h.get('revision_plan')) for h in grounded['revision_memory']['attempts']]
                return result, record
            seen = self.seen_history.get(key, [])
            if visible[:len(seen)] != seen or len(visible) <= len(seen):
                raise ValueError('Revision history must extend the U conversation history')
            public = {k: deepcopy(case[k]) for k in PUBLIC_FIELDS}
            experience, metadata = self.memories[key[1]].snapshot(cid)
            if key not in self.sessions:
                self.sessions[key] = self.session_factory(**self.session_options,
                    session_dir=self.run_path / 'sessions' / cid / key[1])
            session = self.sessions[key]
            if not seen:
                strategy, sample, attempts = build_user_message_blocks(route['document'], public,
                    visible, threat_model='black_box')
                prompt = '\n'.join([strategy] + ([experience] if experience else []) + [sample, attempts])
                prompt_mode = 'full'
            else:
                # Retry can produce multiple outcomes without a generator turn.
                # Include every unseen outcome, then the original newest-result delta.
                earlier = visible[len(seen):-1]
                additions = ('## Additional completed attempts since the previous revision\n\n' +
                    '\n'.join(_format_attempt(h, True) for h in earlier) + '\n\n') if earlier else ''
                prompt = additions + build_delta_block(public, visible, threat_model='black_box',
                                                       experience_block=experience)
                prompt_mode = 'delta'
            prompt = revision_prompt(prompt, self.revision_profile)
            record.update(revision_path='piminer_black_box',
                          prompt_mode=prompt_mode, session_id=session.session_id,
                          revision_profile=self.revision_profile,
                          experience=metadata, history_iters=[h['iter'] for h in visible],
                          new_history_iters=[h['iter'] for h in visible[len(seen):]])
            item = dict(stage='attacker_revise', prompt=prompt, system_prompt=self.revision_role, status='running')
            record['calls'].append(item)
            start = len(session.calls)
            try:
                output = session(prompt, 'attacker_revise')
                item.update(output=output, status='completed')
            except Exception as exc:
                item.update(status='error', error_type=type(exc).__name__)
                raise
            finally:
                item['provider'] = deepcopy(session.calls[-1]) if len(session.calls) > start else None
            self.seen_history[key] = visible
            self.generation_history[key] = visible
            record['candidates'].append(output)
            analysis, injection = router._parse_attempt(output)
            if self.revision_profile == 'calibrated':
                record['revision_audit'] = audit_revision(analysis, route['document'], route['strategy_ids'])
            if not injection.strip():
                injection = output
            result = Payload(revision_id, injection)
            record.update(status='ready', analysis=analysis, payload=result.text, payload_sha256=result.sha256)
            return result, record
        except Exception as exc:
            error_type = exc.record.get('error_type', type(exc).__name__) if isinstance(exc, GenerationError) else type(exc).__name__
            record.update(status='GENERATION_ERROR', error_type=error_type)
            raise GenerationError(record) from exc
