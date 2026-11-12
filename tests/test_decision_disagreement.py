"""Actual shared-prefix comparisons and bounded, explicitly labelled policy replay."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.engine.budget import Budget
from src.engine.history import History
from src.evaluation.decision_disagreement import (
    DecisionCase, load_recorded_cases, pairwise_first_disagreement, replay_first_disagreement)
from src.policies.registry import build_policies
from src.policies.stage_aware import StageAware
from src.schema import Episode, payload_hash


def ep(n=0, pid='shared', trigger='F', gold=False, payload='P0'):
    return Episode(f'c000/{pid}/{n:03d}', 'c000', pid, payload, payload_hash('bytes-'+payload),
        'initial' if n == 0 else 'post_failure',
        'TARGET_ERROR' if gold is None else 'VALID_GOLD_TRUE' if gold else 'VALID_GOLD_FALSE',
        gold, 'PRIVATE_FINAL', trigger,
        'ERROR' if gold is None else 'SUCCESS' if gold else 'TRIGGERED_FAIL' if trigger == 'T'
        else 'NOT_TRIGGERED' if trigger == 'F' else 'UNOBSERVABLE', 'NONE', [],
        {'private_observation': 'PRIVATE_OBSERVATION'}, None,
        'FixtureError' if gold is None else None, None, 1.0, 0,
        f'raw/c000/{pid}/{n:03d}.json')


def decisions(episodes, pid, limit=4, policy=None):
    policy = policy or build_policies({pid: {}})[pid]
    result = []
    for n in range(1, len(episodes)+1):
        history = History.from_episodes(episodes[:n], Budget(limit, n-1))
        decision = policy.decide(history)
        result.append(dict(decision_id=f'c000/{pid}/d{n:03d}', case_id='c000', policy_id=pid,
            visible_history_ids=[e.episode_id for e in episodes[:n]], current_stage=history.last.stage,
            history=history.summary(), remaining_target_budget=history.remaining_target_budget,
            spent_target_budget=n-1, action=decision.action.value, reason=decision.reason))
    return tuple(result)


def case(pid='stage_aware_p0_confirm', episodes=None, source='/source/c000', limit=4):
    episodes = tuple(episodes if episodes is not None else [ep()])
    return DecisionCase('c000', pid, limit+1, source, episodes, decisions(episodes, pid, limit),
        tuple((source, e.episode_id) for e in episodes), tuple(f'state-{i}' for i in range(len(episodes))),
        {'case_id': 'c000'}, 'completed', 'VALID_GOLD_FALSE')


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(v)+'\n' for v in values))


def save_run(path, branches, limit=4):
    manifest = dict(config=dict(policies={pid: {} for pid in branches}, post_failure_target_budget=limit),
                    cases=[dict(case_id='c000', suite_name='fixture')], case_ids=['c000'])
    dump(path/'manifest.json', manifest)
    dump(path/'status.json', dict(status='completed'))
    eps = {e.episode_id: e for entries in branches.values() for e in entries}
    jsonl(path/'episodes.jsonl', [e.to_dict() for e in eps.values()])
    jsonl(path/'decisions.jsonl', [d for pid, entries in branches.items() for d in decisions(entries, pid, limit)])
    events = []
    for e in eps.values():
        dump(path/e.raw_path, dict(payload='bytes-'+e.payload_id, result=dict(gold=e.gold, error=e.error,
             raw={'messages': 'PRIVATE_TRAJECTORY'}), query_packets=[]))
        events.extend([dict(event='TARGET_STARTED', episode_id=e.episode_id, payload_sha256=e.payload_sha256),
                       dict(event='TARGET_COMPLETED', episode_id=e.episode_id, status=e.status)])
    jsonl(path/'events.jsonl', events)
    dump(path/'outcomes.json', [dict(case_id='c000', policy_id=pid, status='VALID_GOLD_FALSE', gold_success=False)
                              for pid in branches])
    return manifest


class DecisionDisagreementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_actual_shared_h1_records_first_disagreement_without_private_text(self):
        run = self.root/'shared'
        save_run(run, {'always_revise': [ep()], 'stage_aware_p0_confirm': [ep()]})
        report = pairwise_first_disagreement(load_recorded_cases(run, 'B0'), load_recorded_cases(run, 'B2'),
                                            left_label='B0', right_label='B2')
        self.assertTrue(report['actual_group_comparison'])
        self.assertEqual(report['mode'], 'actual_recorded_arms')
        row = report['cases'][0]
        self.assertEqual(row['status'], 'first_action_disagreement')
        first = row['first_disagreement']
        self.assertEqual((first['after_target_execution'], first['stage'], first['trigger'],
                          first['remaining_target_budget']), (1, 'NOT_TRIGGERED', 'F', 4))
        self.assertEqual((first['left']['action'], first['right']['action']), ('Revise', 'Retry'))
        self.assertIn('decisions.jsonl', first['left']['source'])
        self.assertNotIn('PRIVATE_', json.dumps(report))
        self.assertNotIn('bytes-P0', json.dumps(report))

    def test_independent_runs_with_identical_payload_gold_trigger_and_raw_are_not_shared(self):
        left, right = self.root/'left', self.root/'right'
        save_run(left, {'always_revise': [ep()]})
        save_run(right, {'stage_aware_p0_confirm': [ep()]})
        row = pairwise_first_disagreement(load_recorded_cases(left), load_recorded_cases(right))['cases'][0]
        self.assertEqual(row['status'], 'history_diverged_before_action_disagreement')
        self.assertEqual(row['divergence_reason'], 'independent_target_execution')
        self.assertIsNone(row['first_disagreement'])
        self.assertEqual(row['compared_decision_boundaries'], 0)

    def test_shared_h1_then_independent_target_trajectories_stop_before_second_action(self):
        run = self.root/'branches'
        save_run(run, {'always_revise': [ep(), ep(1, 'always_revise', 'T', payload='P1')],
                       'stage_aware': [ep(), ep(1, 'stage_aware', 'T', payload='P1')]})
        row = pairwise_first_disagreement(load_recorded_cases(run, 'B0'), load_recorded_cases(run, 'B1'))['cases'][0]
        self.assertEqual(row['status'], 'history_diverged_before_action_disagreement')
        self.assertEqual(row['after_target_execution'], 2)
        self.assertEqual(row['compared_decision_boundaries'], 1)

    def test_observation_drift_on_claimed_same_execution_precedes_action_comparison(self):
        left, right = case('always_revise'), case('stage_aware_p0_confirm')
        right.state_fingerprints = ('changed-observation',)
        row = pairwise_first_disagreement({'c000': left}, {'c000': right})['cases'][0]
        self.assertEqual(row['status'], 'history_diverged_before_action_disagreement')
        self.assertEqual(row['divergence_reason'], 'trajectory_or_observation_changed')

    def test_budget_and_case_identity_must_match(self):
        left = case('always_revise')
        for right, status in [(case(limit=3), 'incomparable_budget'),
                              (replace(case(), identity={'case_id': 'c000', 'different': True}), 'incomparable_case_identity')]:
            with self.subTest(status=status):
                row = pairwise_first_disagreement({'c000': left}, {'c000': right})['cases'][0]
                self.assertEqual(row['status'], status)

    def test_missing_case_history_decision_and_unverified_origin_remain_missing(self):
        left, right = case('always_revise'), case('stage_aware')
        variants = [({}, 'missing_case'),
                    ({'c000': replace(right, episodes=())}, 'missing_history'),
                    ({'c000': replace(right, decisions=())}, 'missing_decision'),
                    ({'c000': replace(right, execution_origins=(None,))}, 'incomparable_unverified_execution_origin')]
        for rhs, status in variants:
            with self.subTest(status=status):
                self.assertEqual(pairwise_first_disagreement({'c000': left}, rhs)['cases'][0]['status'], status)

    def test_equal_nonstop_prefix_without_next_execution_is_incomplete(self):
        row = pairwise_first_disagreement({'c000': case('always_revise')}, {'c000': case('stage_aware')})['cases'][0]
        self.assertEqual(row['status'], 'incomplete_after_shared_prefix')
        self.assertIsNone(row['first_disagreement'])

    def test_equal_terminal_stop_is_not_disagreement(self):
        left, right = case('always_revise', [ep(gold=True)]), case('stage_aware', [ep(gold=True)])
        row = pairwise_first_disagreement({'c000': left}, {'c000': right})['cases'][0]
        self.assertEqual(row['status'], 'no_action_disagreement_at_common_stop')

    def test_generation_error_stop_does_not_replace_actual_revise_decision(self):
        left, right = case('always_revise'), case('stage_aware')
        error = dict(right.decisions[0], decision_id=right.decisions[0]['decision_id']+'/error',
                     action='Stop', reason='GENERATION_ERROR')
        right.decisions += (error,)
        row = pairwise_first_disagreement({'c000': left}, {'c000': right})['cases'][0]
        self.assertEqual(row['status'], 'incomplete_after_shared_prefix')

    def test_duplicate_or_wrong_budget_decision_is_not_compared(self):
        for mutation in ('duplicate', 'budget'):
            right = case('stage_aware')
            if mutation == 'duplicate':right.decisions += right.decisions
            else:right.decisions[0]['remaining_target_budget'] = 100
            row = pairwise_first_disagreement({'c000': case('always_revise')}, {'c000': right})['cases'][0]
            self.assertEqual(row['status'], 'incomparable_invalid_decision')

    def test_replay_is_marked_and_stops_at_first_pair_disagreement(self):
        observed = case(episodes=[ep(), ep(1, 'stage_aware_p0_confirm', 'T', payload='P0')])
        report = replay_first_disagreement({'c000': observed}, 'B1', 'B2', left_label='B1', right_label='B2')
        self.assertEqual(report['mode'], 'replay_on_observed_history')
        self.assertFalse(report['actual_group_comparison'])
        self.assertFalse(report['counterfactual_future_generated'])
        row = report['cases'][0]
        self.assertEqual(row['status'], 'first_action_disagreement')
        self.assertEqual(row['first_disagreement']['after_target_execution'], 1)
        self.assertEqual(row['compared_decision_boundaries'], 1)

    def test_replay_stops_when_pair_common_action_leaves_source_trajectory(self):
        observed = case(episodes=[ep(), ep(1, 'stage_aware_p0_confirm', 'T', payload='P0')])
        row = replay_first_disagreement({'c000': observed}, 'B0', 'B1')['cases'][0]
        self.assertEqual(row['status'], 'observed_action_diverged_before_pair_disagreement')
        self.assertEqual(row['after_target_execution'], 1)
        self.assertEqual(row['compared_decision_boundaries'], 1)
        self.assertEqual(row['observed_action']['action'], 'Retry')
        self.assertIsNone(row['first_disagreement'])

    def test_replay_preserves_custom_policy_objects_and_config_parameters(self):
        observed = case('stage_aware', [ep(trigger='T')])
        for custom in (StageAware(max_retries_per_payload=0), {'stage_aware': {'max_retries_per_payload': 0}}):
            row = replay_first_disagreement({'c000': observed}, custom, 'stage_aware')['cases'][0]
            self.assertEqual(row['first_disagreement']['left']['action'], 'Revise')
            self.assertEqual(row['first_disagreement']['right']['action'], 'Retry')

    def test_replay_uses_only_consecutive_source_compatible_prefixes(self):
        observed = case(episodes=[ep(trigger='U'), ep(1, 'stage_aware_p0_confirm', 'F', payload='P0')])
        row = replay_first_disagreement({'c000': observed}, 'B1', 'B2')['cases'][0]
        self.assertEqual(row['status'], 'incomplete_observed_history')
        self.assertEqual(row['compared_decision_boundaries'], 2)

    def test_replay_missing_observed_action_is_not_a_future_trajectory(self):
        observed = replace(case('stage_aware', [ep(trigger='T')]), decisions=())
        row = replay_first_disagreement({'c000': observed}, 'B1', 'B2')['cases'][0]
        self.assertEqual(row['status'], 'missing_observed_action')

    def test_loader_rejects_raw_tampering_and_tolerates_incomplete_tail(self):
        run = self.root/'source'
        save_run(run, {'stage_aware': [ep()]})
        with (run/'episodes.jsonl').open('a') as handle:handle.write('{"episode_id":')
        loaded = load_recorded_cases(run)['c000']
        self.assertEqual(len(loaded.episodes), 1)
        self.assertTrue(loaded.warnings)
        raw = json.loads((run/ep().raw_path).read_text());raw['payload'] = 'tampered'
        dump(run/ep().raw_path, raw)
        loaded = load_recorded_cases(run)
        self.assertTrue(loaded['c000'].issues)
        self.assertEqual(replay_first_disagreement(loaded, 'B0', 'B2')['cases'][0]['status'], 'incomparable_invalid_records')

    def test_explicit_verified_initial_reuse_is_shared_but_unproven_copy_is_not(self):
        source, copied = self.root/'original/c000', self.root/'copy/c000'
        save_run(source, {'always_revise': [ep()]})
        manifest = save_run(copied, {'stage_aware_p0_confirm': [ep()]})
        raw_bytes = (source/ep().raw_path).read_bytes()
        manifest.update(reused_initial=True, source_run=str(source.parent),
                        initial_provenance=dict(source_case=str(source), episode_id=ep().episode_id))
        dump(copied/'manifest.json', manifest)
        jsonl(copied/'events.jsonl', [dict(event='INITIAL_EXECUTION_REUSED', episode_id=ep().episode_id,
              source_sha256=hashlib.sha256(raw_bytes).hexdigest())])
        left = load_recorded_cases(source)
        row = pairwise_first_disagreement(left, load_recorded_cases(copied))['cases'][0]
        self.assertEqual(row['status'], 'first_action_disagreement')
        manifest.pop('initial_provenance')
        dump(copied/'manifest.json', manifest)
        row = pairwise_first_disagreement(left, load_recorded_cases(copied))['cases'][0]
        self.assertEqual(row['status'], 'incomparable_unverified_execution_origin')

    def test_loader_requires_explicit_policy_for_multi_arm_run(self):
        run = self.root/'multi'
        save_run(run, {'always_revise': [ep()], 'stage_aware': [ep()]})
        with self.assertRaises(ValueError):load_recorded_cases(run)
        with self.assertRaises(ValueError):load_recorded_cases(run, 'B2')

    def test_child_manifest_budget_and_identity_are_checked(self):
        run = self.root/'parent'
        child_manifest = save_run(run/'c000', {'stage_aware': [ep()]}, limit=3)
        parent_manifest = deepcopy(child_manifest)
        parent_manifest['config']['post_failure_target_budget'] = 4
        dump(run/'manifest.json', parent_manifest)
        loaded = load_recorded_cases(run)
        self.assertIn('child_manifest_mismatch', loaded['c000'].issues)
        self.assertEqual(replay_first_disagreement(loaded, 'B0', 'B2')['cases'][0]['status'], 'incomparable_invalid_records')

    def test_not_started_case_is_preserved(self):
        run = self.root/'pending'
        dump(run/'manifest.json', dict(config=dict(policies={'stage_aware': {}}, post_failure_target_budget=4),
             case_ids=['c000'], cases=[dict(case_id='c000')]))
        loaded = load_recorded_cases(run)
        self.assertEqual(loaded['c000'].recording_status, 'not_started')
        self.assertEqual(replay_first_disagreement(loaded, 'B0', 'B1')['cases'][0]['status'], 'missing_history')


if __name__ == '__main__':
    unittest.main()
