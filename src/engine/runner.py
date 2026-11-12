"""The only execution loop. Policies decide actions; adapters perform them."""
from copy import deepcopy
import random
from ..schema import Action, Episode, Payload, TargetResult
from ..attacker.piminer import GenerationError
from ..evidence.ledger import EvidenceLedger
from ..evidence.stage import estimate_stage, response_signal
from .budget import Budget
from .history import History
from ..evaluation.beta_bernoulli import trigger_statistics_report
from .storage import write_json
from ..evaluation.metrics import summarize


class Runner:
    def __init__(self, target, attacker, observer, policies, store, *, post_failure_budget=3, seed=0):
        self.target, self.attacker, self.observer = target, attacker, observer
        self.policies, self.store = policies, store
        self.limit, self.seed = post_failure_budget, seed
        Budget(self.limit)
        self.episodes, self.decisions, self.outcomes, self.generations = [], [], [], []
        self.packets = {}
        self.attempts = {}
        self.payload_analyses = {}
        self.payload_plans = {}
        if hasattr(self.attacker, 'bind_run'):
            self.attacker.bind_run(self.store.path, self.policies)

    def _generation_history(self, history):
        """Project completed branch evidence; never expose raw Target state.

        Original and U adapters keep their existing black-box field whitelist.
        Grounded revisions may additionally consume this episode-scoped view.
        """
        completed = []
        for index, episode in enumerate(history):
            observation = dict(episode_id=episode.episode_id, case_id=episode.case_id,
                policy_id=episode.policy_id, payload_sha256=episode.payload_sha256,
                trigger=episode.trigger, observation_error=episode.observation_error is not None,
                predicates=[{k: deepcopy(p[k]) for k in ('id', 'query', 'status', 'is_trigger')}
                            for p in episode.evidence],
                query_results=[dict(query_id=p['query_id'], response={
                    k: deepcopy(p['response'][k]) for k in ('supported', 'evidence')})
                    for p in self.packets.get(episode.episode_id, ())])
            completed.append(dict(self.attempts[episode.episode_id], iter=index,
                                  revision_observation=observation))
        return completed

    def execute(self, case, policy_id, payload, phase, ordinal):
        eid = f"{case['case_id']}/{policy_id}/{ordinal:03d}"
        self.store.append('events', dict(event='TARGET_STARTED', episode_id=eid,
                                         payload_sha256=payload.sha256))
        try:
            result = self.target.execute(deepcopy(case), payload, eid)
            if not isinstance(result, TargetResult):
                raise TypeError('Adapter did not return TargetResult')
        except Exception as exc:
            result = TargetResult(gold=None, error=type(exc).__name__)
        packets = ()
        ledger = EvidenceLedger(eid, case['goal_spec'])
        if result.error is None:
            try:
                packets = self.observer.collect(eid, case, result.observation)
                for p in packets:
                    ledger.update(p)
            except Exception as exc:
                # Observation failure never erases a valid official Gold result.
                result.observation_error = type(exc).__name__
                result.observation = None
                packets = ()
                ledger = EvidenceLedger(eid, case['goal_spec'])
        raw_path = self.store.raw(eid + '.json', dict(payload=payload.text, result=result,
                                                    query_packets=packets))
        self.packets[eid] = packets
        trigger = ledger.trigger
        episode = Episode(eid, case['case_id'], policy_id, payload.id, payload.sha256, phase,
                          'TARGET_ERROR' if result.error else 'VALID_GOLD_TRUE' if result.gold else 'VALID_GOLD_FALSE',
                          result.gold, result.final, trigger, estimate_stage(result.gold, trigger, result.error),
                          response_signal(result.final), ledger.records(), result.observation, result.usage,
                          result.error, result.observation_error, result.seconds,
                          sum(p['charged_cost'] for p in packets), raw_path)
        self.episodes.append(episode)
        analysis_key = (case['case_id'], policy_id, payload.id)
        shared_key = (case['case_id'], 'shared', payload.id)
        self.attempts[eid] = dict(analysis=self.payload_analyses.get(
            analysis_key, self.payload_analyses.get(shared_key, '')),
            injection=payload.text, security=result.gold, utility=result.utility,
            final_output=(result.black_box_final if result.black_box_final is not None else result.final)[:4000])
        plan = self.payload_plans.get(analysis_key, self.payload_plans.get(shared_key))
        if plan is not None:
            self.attempts[eid]['revision_plan'] = deepcopy(plan)
        self.store.append('episodes', episode)
        self.store.append('events', dict(event='TARGET_COMPLETED', episode_id=eid, status=episode.status))
        return episode

    def generate(self, case, policy_id, payload, last, revision_id, history=()):
        gid = f"{case['case_id']}/{policy_id}/{revision_id}"
        self.store.append('events', dict(event='GENERATION_STARTED', generation_id=gid))
        try:
            args = (deepcopy(case), payload, last,
                    self.packets.get(last.episode_id, ()) if last else (), revision_id)
            if hasattr(self.attacker, 'generate_with_observed_history'):
                completed = self._generation_history(history)
                result, record = self.attacker.generate_with_observed_history(*args, completed)
            elif hasattr(self.attacker, 'generate_with_history'):
                completed = [dict(self.attempts[e.episode_id], iter=i) for i, e in enumerate(history)]
                result, record = self.attacker.generate_with_history(*args, completed)
            else:
                result, record = self.attacker.generate(*args)
        except GenerationError as exc:
            result, record = None, exc.record
        except Exception as exc:
            result, record = None, dict(status='GENERATION_ERROR', error_type=type(exc).__name__, calls=[])
        policy = self.policies.get(policy_id)
        if (result is not None and history and revision_id != 'P0'
                and getattr(policy, 'reject_initial_payload_revisions', False)
                and result.sha256 == history[0].payload_sha256):
            # Renaming the original bytes P1/P2 is not a revised candidate.
            # Preserve the paid generation record, but do not reserve Target
            # budget or silently regenerate. The existing error path stops.
            record = dict(record, status='GENERATION_ERROR',
                          error_type='RevisionMatchesInitialPayload',
                          rejected_payload_sha256=result.sha256)
            result = None
        record = dict(record, case_id=case['case_id'], policy_id=policy_id, generation_id=gid)
        self.generations.append(record)
        if result is not None:
            self.payload_analyses[case['case_id'], policy_id, revision_id] = record.get('analysis', '')
            if record.get('revision_plan') is not None:
                self.payload_plans[case['case_id'], policy_id, revision_id] = deepcopy(record['revision_plan'])
        path = self.store.raw(gid + '.generation.json', record)
        self.store.append('events', dict(event='GENERATION_COMPLETED', generation_id=gid,
                                         status=record['status'], raw_path=path))
        return result, record

    def _case(self, case):
        p0 = (Payload('P0', case['p0']) if case.get('p0')
              and getattr(self.attacker, 'use_cached_p0', True) else None)
        if p0 is None:
            p0, _ = self.generate(case, 'shared', None, None, 'P0')
        if p0 is None:
            for policy_id in self.policies:
                self.outcomes.append(dict(case_id=case['case_id'], policy_id=policy_id,
                                         initial_gold=None, gold_success=None, status='GENERATION_ERROR',
                                         target_attempts=0, error_phase='initial'))
            return
        h1 = self.execute(case, 'shared', p0, 'initial', 0)
        order = list(self.policies)
        # Deterministic counterbalancing limits fixed arm-order/time confounding.
        random.Random(f'{self.seed}:{case["case_id"]}').shuffle(order)
        for policy_id in order:
            policy = self.policies[policy_id]
            if hasattr(self.attacker, 'begin_branch'):
                self.attacker.begin_branch(case, policy_id)
            budget = Budget(self.limit)
            history, payload, revisions = [h1], p0, 0
            status = h1.status
            while True:
                visible = History.from_episodes(history, budget)
                decision = policy.decide(visible)
                row = dict(decision_id=f'{case["case_id"]}/{policy_id}/d{len(history):03d}',
                           case_id=case['case_id'], policy_id=policy_id,
                           visible_history_ids=[e.episode_id for e in history],
                           current_stage=history[-1].stage, history=visible.summary(),
                           remaining_target_budget=budget.remaining, spent_target_budget=budget.spent,
                           action=decision.action.value, reason=decision.reason)
                # Compute only after the action is fixed. Never expose this to
                # History, policy input, or the Attacker's revision messages.
                row['trigger_posterior'] = trigger_statistics_report(
                    e for e in history if e.payload_sha256 == history[-1].payload_sha256)
                self.decisions.append(row)
                self.store.append('decisions', row)
                if decision.action == Action.STOP:
                    break
                if decision.action == Action.REVISE:
                    revisions += 1
                    revised, _ = self.generate(case, policy_id, payload, history[-1], f'P{revisions}', history)
                    if revised is None:
                        status = 'GENERATION_ERROR'
                        error_decision = dict(row, decision_id=row['decision_id'] + '/error',
                                              action='Stop', reason='GENERATION_ERROR')
                        self.decisions.append(error_decision)
                        self.store.append('decisions', error_decision)
                        break
                    payload = revised
                budget.reserve()
                last = self.execute(case, policy_id, payload, 'post_failure', budget.spent)
                history.append(last)
                status = last.status
            success = any(e.gold is True for e in history)
            invalid = status in ('TARGET_ERROR', 'GENERATION_ERROR')
            self.outcomes.append(dict(case_id=case['case_id'], policy_id=policy_id, initial_gold=h1.gold,
                                     gold_success=True if success else None if invalid else False,
                                     status=status, target_attempts=budget.spent,
                                     shared_h1_id=h1.episode_id, revisions=revisions))
            if hasattr(self.attacker, 'finish_branch'):
                # Persist the terminal result before making it visible to siblings.
                write_json(self.store.path / 'outcomes.json', self.outcomes)
                completed = [dict(self.attempts[e.episode_id], iter=i) for i, e in enumerate(history)]
                publication = self.attacker.finish_branch(case, policy_id, completed, self.outcomes[-1])
                self.store.append('events', dict(event='EXPERIENCE_COMMITTED', case_id=case['case_id'],
                    policy_id=policy_id, **publication))

    def run(self, cases):
        write_json(self.store.path / 'status.json', dict(status='running'))
        try:
            for case in cases:
                self._case(case)
                write_json(self.store.path / 'outcomes.json', self.outcomes)
                write_json(self.store.path / 'metrics.json', self.metrics())
            write_json(self.store.path / 'status.json', dict(status='completed'))
        except BaseException as exc:
            # Never resume an uncertain started request by silently resampling it.
            write_json(self.store.path / 'status.json', dict(status='interrupted', error_type=type(exc).__name__,
                                                           recovery='Inspect events; use a new run directory.'))
            raise
        return self.metrics()

    def metrics(self):
        return summarize(self.episodes, self.outcomes, self.generations, self.decisions, self.limit)
