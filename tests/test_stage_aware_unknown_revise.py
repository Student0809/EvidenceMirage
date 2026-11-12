"""Unknown can buy a fresh candidate while positive evidence preserves payloads."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from src.engine.config import ROOT, load_cases, load_config
from src.engine.history import History, ObservationSummary
from src.engine.runner import Runner
from src.engine.storage import RunStore
from src.evidence.observer import FixedObserver, policy_for_case
from src.evidence.stage import estimate_stage
from src.policies.registry import build_policies
from src.policies.stage_aware import StageAware
from src.schema import Action, Payload, TargetResult
from src.vendor.attacker_observer import Collector


def history(states, *, hashes=None, ids=None, remaining=4):
    count = len(states)
    hashes = hashes or ['same'] * count
    ids = ids or ['P0'] * count
    return History(tuple(
        ObservationSummary(str(i), sha, estimate_stage(False, state), state,
                           'Reject', payload_id=pid)
        for i, (state, sha, pid) in enumerate(zip(states, hashes, ids))
    ), remaining, count - 1)


class UnknownRevisePolicyTests(unittest.TestCase):
    def test_latest_unknown_revises_even_after_positive_or_negative_evidence(self):
        policy = StageAware(unknown_action='revise')
        for states in (['U'], ['U', 'U'], ['T', 'U'], ['F', 'U']):
            with self.subTest(states=states):
                decision = policy.decide(history(states))
                self.assertEqual(decision.action, Action.REVISE)
                self.assertEqual(decision.reason, 'unknown_piminer_revise')

    def test_unknown_revision_reason_is_preserved_at_payload_retry_cap(self):
        for cap in (0, 2):
            with self.subTest(cap=cap):
                decision = StageAware(max_retries_per_payload=cap,
                                      unknown_action='revise').decide(history(['U'] * 3))
                self.assertEqual(decision.action, Action.REVISE)
                self.assertEqual(decision.reason, 'unknown_piminer_revise')

    def test_observation_error_with_valid_failed_target_revises(self):
        observed = history(['U'])
        observed = replace(observed, completed=(replace(observed.last,
                                                       observation_error=True),))
        decision = StageAware(unknown_action='revise').decide(observed)
        self.assertEqual(decision.action, Action.REVISE)
        self.assertEqual(decision.reason, 'unknown_piminer_revise')

    def test_positive_trigger_retries_current_payload_with_existing_cap(self):
        policy = StageAware(unknown_action='revise')
        for states in (['T'], ['U', 'T'], ['T', 'T']):
            with self.subTest(states=states):
                decision = policy.decide(history(states))
                self.assertEqual(decision.action, Action.RETRY)
                self.assertEqual(decision.reason, 'preserve_observed_trigger')
        decision = policy.decide(history(['T'] * 3))
        self.assertEqual(decision.action, Action.REVISE)
        self.assertEqual(decision.reason, 'payload_retry_cap')

    def test_payload_renaming_does_not_reset_sha_based_positive_retry_cap(self):
        policy = StageAware(unknown_action='revise')
        same_bytes = history(['T'] * 3, ids=['P0', 'P1', 'P2'])
        self.assertEqual(policy.decide(same_bytes).reason, 'payload_retry_cap')
        different_bytes = history(['T'] * 3, hashes=['initial', 'initial', 'new'],
                                  ids=['P0', 'P0', 'P1'])
        self.assertEqual(policy.decide(different_bytes).action, Action.RETRY)

    def test_negative_trigger_preserves_existing_revision_reasons(self):
        policy = StageAware(unknown_action='revise')
        self.assertEqual(policy.decide(history(['F'])).reason, 'trigger_false')
        decision = policy.decide(history(['T', 'F']))
        self.assertEqual(decision.action, Action.REVISE)
        self.assertEqual(decision.reason, 'trigger_false_after_retry')

    def test_success_error_and_budget_stops_override_unknown_mode(self):
        policy = StageAware(unknown_action='revise')
        for stage, reason in (('SUCCESS', 'official_gold_true'),
                              ('ERROR', 'target_error_not_gold_false')):
            with self.subTest(stage=stage):
                observed = history(['U'])
                observed = replace(observed, completed=(replace(observed.last, stage=stage),))
                decision = policy.decide(observed)
                self.assertEqual(decision.action, Action.STOP)
                self.assertEqual(decision.reason, reason)
        decision = policy.decide(history(['U'], remaining=0))
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, 'target_budget_exhausted')

    def test_registry_accepts_explicit_revision_mode(self):
        policy = build_policies({'stage_aware': {'unknown_action': 'revise'}})['stage_aware']
        self.assertEqual(policy.decide(history(['U'])).reason, 'unknown_piminer_revise')

    def test_invalid_unknown_actions_are_rejected(self):
        for value in ('Retry', 'Revise', 'unknown', '', None, True, 0, []):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    StageAware(unknown_action=value)

    def test_legacy_schedule_remains_default_with_original_reasons(self):
        policies = (StageAware(), StageAware(unknown_action='schedule'))
        for policy in policies:
            for count, expected in ((1, Action.RETRY), (2, Action.REVISE)):
                with self.subTest(policy=policy, count=count):
                    decision = policy.decide(history(['U'] * count))
                    self.assertEqual(decision.action, expected)
                    self.assertEqual(decision.reason, 'unknown_fixed_schedule')
            self.assertEqual(policy.decide(history(['U'] * 3)).reason, 'payload_retry_cap')
        explicit = StageAware(unknown_schedule=['Revise', 'Retry'])
        self.assertEqual(explicit.decide(history(['U'])).action, Action.REVISE)
        self.assertEqual(explicit.decide(history(['U', 'U'])).action, Action.RETRY)


class SequenceTarget:
    """Use legal observer packets rather than supplying Trigger labels to Runner."""

    def __init__(self, states):
        self.states = states
        self.calls = []

    def execute(self, case, payload, episode_id):
        state = self.states[min(len(self.calls), len(self.states) - 1)]
        self.calls.append((episode_id, payload.id, payload.text, payload.sha256))
        collector = Collector(policy_for_case(case))
        if state == 'T':
            collector.mail(collector.policy['mailboxes'], 'subject', 'body', ['file'])
        final = 'offline target execution ' + str(len(self.calls))
        return TargetResult(state == 'success', final=final,
                            observation=collector.finish(final))


class HistoryAttacker:
    def __init__(self):
        self.calls = []

    def generate_with_history(self, case, payload, episode, packets, revision_id, completed):
        self.calls.append(dict(payload=payload, episode=episode, packets=deepcopy(packets),
                               revision_id=revision_id, completed=deepcopy(completed)))
        candidate = Payload(revision_id, 'distinct offline candidate ' + revision_id)
        return candidate, dict(status='ready', calls=[], analysis='analysis for ' + revision_id)


class UnknownReviseRunnerTests(unittest.TestCase):
    def run_sequence(self, states):
        # This fixture has a mail/attachment Trigger reachable by FixedObserver.
        case = load_cases(load_config(ROOT / 'experiments/e0_smoke.yaml'))[5]
        case['p0'] = 'fixed initial offline candidate'
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target, attacker = SequenceTarget(states), HistoryAttacker()
        runner = Runner(target, attacker, FixedObserver(),
                        {'stage_aware': StageAware(unknown_action='revise')},
                        RunStore(Path(directory.name) / 'run', {}), post_failure_budget=4)
        metrics = runner.run([case])
        return runner, target, attacker, metrics

    def test_all_unknown_generates_four_candidates_with_complete_failure_history(self):
        runner, target, attacker, metrics = self.run_sequence(['U'] * 5)
        self.assertEqual([call[1] for call in target.calls], ['P0', 'P1', 'P2', 'P3', 'P4'])
        self.assertEqual(len({call[0] for call in target.calls}), 5)
        self.assertEqual(len({call[3] for call in target.calls}), 5)
        self.assertEqual([episode.trigger for episode in runner.episodes], ['U'] * 5)
        self.assertEqual(len(attacker.calls), 4)
        for count, call in enumerate(attacker.calls, 1):
            with self.subTest(revision=count):
                self.assertEqual(call['revision_id'], 'P' + str(count))
                self.assertEqual(call['payload'].text, target.calls[count - 1][2])
                self.assertEqual(call['episode'].episode_id, target.calls[count - 1][0])
                self.assertEqual([attempt['iter'] for attempt in call['completed']], list(range(count)))
                self.assertEqual([attempt['injection'] for attempt in call['completed']],
                                 [entry[2] for entry in target.calls[:count]])
                self.assertTrue(all(attempt['security'] is False for attempt in call['completed']))
                self.assertEqual([attempt['final_output'] for attempt in call['completed']],
                                 ['offline target execution ' + str(i + 1) for i in range(count)])
        self.assertEqual([row['reason'] for row in runner.decisions],
                         ['unknown_piminer_revise'] * 4 + ['target_budget_exhausted'])
        self.assertEqual(metrics['physical_total_cost']['target_executions'], 5)
        self.assertEqual(runner.outcomes[0]['target_attempts'], 4)
        self.assertEqual(runner.outcomes[0]['revisions'], 4)
        self.assertIs(runner.outcomes[0]['gold_success'], False)

    def test_positive_retry_then_unknown_revision_passes_both_completed_rollouts(self):
        runner, target, attacker, metrics = self.run_sequence(['T', 'U', 'success'])
        self.assertEqual([call[1] for call in target.calls], ['P0', 'P0', 'P1'])
        self.assertEqual(target.calls[0][3], target.calls[1][3])
        self.assertNotEqual(target.calls[1][3], target.calls[2][3])
        self.assertEqual([episode.trigger for episode in runner.episodes], ['T', 'U', 'U'])
        self.assertEqual([row['action'] for row in runner.decisions], ['Retry', 'Revise', 'Stop'])
        self.assertEqual([row['reason'] for row in runner.decisions],
                         ['preserve_observed_trigger', 'unknown_piminer_revise', 'official_gold_true'])
        self.assertEqual(len(attacker.calls), 1)
        completed = attacker.calls[0]['completed']
        self.assertEqual([attempt['iter'] for attempt in completed], [0, 1])
        self.assertEqual([attempt['injection'] for attempt in completed], [target.calls[0][2]] * 2)
        self.assertEqual([attempt['final_output'] for attempt in completed],
                         ['offline target execution 1', 'offline target execution 2'])
        self.assertTrue(all(attempt['security'] is False for attempt in completed))
        self.assertEqual(metrics['physical_total_cost']['target_executions'], 3)
        self.assertEqual(runner.outcomes[0]['revisions'], 1)
        self.assertIs(runner.outcomes[0]['gold_success'], True)


if __name__ == '__main__':
    unittest.main()
