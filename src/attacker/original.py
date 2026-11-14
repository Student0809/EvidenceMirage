"""Original PIMiner Router and Attacker prompts, controlled by the shared Runner.

Routing is shared once per case. Retry has no model call; Revise uses the original
full black-box history prompt, without evidence or refinement-plan additions.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

from .piminer import GenerationError, PUBLIC_FIELDS
from .transport import RoleDecision
from .revision import audit_revision, revision_prompt, revision_role
from ..schema import Payload
from ..vendor import router
from ..vendor.prompts import build_user_message_blocks


class OriginalPIMinerAdapter:
    use_cached_p0 = False
    mode = 'original'

    def __init__(self, root, model, target_model, effort='max', timeout=900, decide=None,
                 route_decide=None, revision_profile='original', *,
                 ablation=None, judgment_memory=None):
        from ..feedback.ablation import AssessmentModules
        self.assessment_modules = AssessmentModules(ablation, judgment_memory)
        self.root = Path(root).resolve()
        if self.root != router.PROJECT_ROOT.resolve():
            raise ValueError('Original router must use the frozen project strategy library')
        self.target_model = target_model
        self.role = (self.root / 'src/vendor/attacker_role.txt').read_text()
        self.revision_profile = revision_profile
        self.revision_role = revision_role(self.role, revision_profile)
        self.decide = decide or RoleDecision(model, effort, timeout, self.role)
        from ..feedback.assessment import SYSTEM
        self.autonomous_decide = decide or RoleDecision(model, effort, timeout, SYSTEM)
        self.revision_decide = (self.decide if revision_profile == 'original' or decide is not None
                                else RoleDecision(model, effort, timeout, self.revision_role))
        # route-next already includes the complete original router role in its body.
        self.route_decide = route_decide or decide or RoleDecision(model, effort, timeout, '')
        self.routes = {}

    def assess_and_decide(self, attempt, history, budget, *, feedback_view='final_only'):
        return self.assessment_modules.assess(attempt, history, budget, self.autonomous_decide,
                                              feedback_view=feedback_view)

    def revise(self, attempt, history, decision, *, feedback_view='final_only', sampling=None):
        from ..feedback.methods import revise_original
        attempt, history = self.assessment_modules.project(attempt, history)
        return revise_original(attempt, history, decision, self.autonomous_decide,
                               feedback_view=feedback_view, sampling=sampling)

    def _validate_case(self, case):
        """Optional resource validation before any model request."""

    def _compose_strategy(self, case, strategies):
        paths = [s['path'] for s in strategies]
        return router._load_sample_strategy(self.root, dict(strategy_paths=paths), None), {}

    def preflight(self):
        results = {}
        for name, model in (('router', self.route_decide), ('attacker', self.decide)):
            start = len(model.calls)
            model('Reply with OK. This is an API connectivity check.', 'preflight_' + name)
            results[name] = dict(status='passed', calls=deepcopy(model.calls[start:]))
        return dict(status='passed', **results)

    def generate(self, case, payload, episode, packets, revision_id, sampling=None):
        return self.generate_with_history(case, payload, episode, packets, revision_id, (),
                                          sampling=sampling)

    def generate_with_history(self, case, payload, episode, packets, revision_id, history,
                              sampling=None):
        record = dict(status='running', mode=self.mode, calls=[], candidates=[], checks=[])
        if sampling is not None:
            record['sampling'] = sampling.public()

        def call(model, prompt, stage, system_prompt):
            item = dict(stage=stage, prompt=prompt, system_prompt=system_prompt, status='running')
            record['calls'].append(item)
            start = len(getattr(model, 'calls', []))
            try:
                output = model(prompt, stage)
                item.update(output=output, status='completed')
                return output
            except Exception as exc:
                item.update(status='error', error_type=type(exc).__name__)
                raise
            finally:
                calls = getattr(model, 'calls', [])
                item['provider'] = deepcopy(calls[-1]) if len(calls) > start else None

        try:
            self._validate_case(case)
            public = {key: deepcopy(case[key]) for key in PUBLIC_FIELDS}
            if payload is not None:
                if episode.gold is not False or episode.error:
                    raise ValueError('Revise requires a completed valid failure')
                if not history or history[-1]['injection'] != payload.text:
                    raise ValueError('Original Attacker requires the completed branch history')
            if not self.assessment_modules.config.historical_experience:
                from ..feedback.ablation import NO_HISTORY
                self.routes[case['case_id']] = dict(strategy_ids=[], strategy_paths=[],
                    strategy_sha256={}, document=NO_HISTORY)
            if case['case_id'] not in self.routes:
                if payload is not None:
                    raise ValueError('Route and generate a shared P0 before revising')
                strategies = router._discover_strategies('agentdojo')
                if not strategies:
                    raise ValueError('Original strategy library is empty and has no template')
                prompt = router.ROUTER_ROLE_PROMPT + '\n\n' + router._format_routing_prompt(
                    self.target_model, public, strategies)
                output = call(self.route_decide, prompt, 'router', '')
                ids = router._parse_choices(output, [s['id'] for s in strategies])
                if not ids:
                    raise ValueError('Router returned no valid strategy IDs')
                by_id = {s['id']: s for s in strategies}
                paths = [by_id[sid]['path'] for sid in ids]
                # The default retains every original byte. A2 changes only this resource view.
                document, resource_metadata = self._compose_strategy(case, [by_id[sid] for sid in ids])
                self.routes[case['case_id']] = dict(strategy_ids=ids,
                    strategy_paths=[str(Path(p).relative_to(self.root)) for p in paths],
                    strategy_sha256={str(Path(p).relative_to(self.root)):
                                     hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},
                    document=document, **resource_metadata)
            route = self.routes[case['case_id']]
            record['routing'] = {k: deepcopy(v) for k, v in route.items() if k != 'document'}
            # Whitelist the original black-box fields: no raw tool results, evidence,
            # other policies' histories, or private environment state enters the prompt.
            visible = [{k: deepcopy(h[k]) for k in
                        ('iter', 'analysis', 'injection', 'security', 'utility', 'final_output')}
                       for h in history]
            prompt = '\n'.join(build_user_message_blocks(route['document'], public, visible,
                                                         threat_model='black_box'))
            if sampling is not None:
                if sampling.config.get('protocol') == 'task_feedback_sampling_v1':
                    plan = sampling.execution_plan(public)
                    prompt += '\n' + sampling.prompt_block(public)
                    if plan is not None:
                        record['task_execution_plan'] = deepcopy(plan)
                else:
                    prompt += '\n' + sampling.prompt_block()
            profile = self.revision_profile if payload is not None else 'original'
            record['revision_profile'] = profile
            prompt = revision_prompt(prompt, profile)
            output = call(self.decide if payload is None else self.revision_decide, prompt,
                          'attacker_p0' if payload is None else 'attacker_revise',
                          self.role if payload is None else self.revision_role)
            record['candidates'].append(output)
            analysis, injection = router._parse_attempt(output)
            if profile == 'calibrated':
                record['revision_audit'] = audit_revision(analysis, route['document'], route['strategy_ids'])
            # Same fallback as the original submit command.
            if not injection.strip():
                injection = output
            if sampling is not None and sampling.config.get('execution_workflow') is not None:
                sampling.validate_payload(injection)
            result = Payload(revision_id, injection)
            record.update(status='ready', analysis=analysis, payload=result.text, payload_sha256=result.sha256)
            return result, record
        except Exception as exc:
            record.update(status='GENERATION_ERROR', error_type=type(exc).__name__)
            raise GenerationError(record) from exc
