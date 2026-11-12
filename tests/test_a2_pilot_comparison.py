"""Offline reporting regressions: preserve missing usage and physical call counts."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.compare_a2_observable_pilot5_20261029 import (
    generation_records, generation_summary, summarize, outcome_sensitivity,
    initial_payload_fingerprints)


class A2PilotComparisonTests(unittest.TestCase):
    def test_incomplete_outcomes_do_not_establish_a2_regression(self):
        def report(values):
            return dict(policies={'stage_aware': {}}, cases=[dict(case_id=cid,
                arms={'stage_aware': {'gold_success': gold}}) for cid, gold in values.items()])
        old = report({'a': False, 'b': True, 'c': None})
        new = report({'a': False, 'b': None, 'c': None})
        row = outcome_sensitivity(new, old)['stage_aware']
        self.assertEqual(row['original']['success_fraction_bounds'], [1 / 3, 2 / 3])
        self.assertEqual(row['a2']['success_fraction_bounds'], [0, 2 / 3])
        self.assertEqual(row['a2_minus_original_fraction_bounds'], [-2 / 3, 1 / 3])
        self.assertEqual(row['both_outcomes_known'], 1)
        self.assertEqual(row['original_only_success'], 0)
        self.assertEqual(row['a2_only_success'], 0)
        self.assertFalse(row['causal_effect_identified'])

    def test_missing_cases_remain_unknown_and_empty_has_no_rate(self):
        new = dict(policies={'p': {}}, cases=[])
        old = dict(policies={'p': {}}, cases=[dict(case_id='a', arms={'p': {'gold_success': False}})])
        row = outcome_sensitivity(new, old)['p']
        self.assertEqual(row['a2']['unknown'], 1)
        self.assertEqual(row['a2']['success_fraction_bounds'], [0, 1])
        empty = outcome_sensitivity(new, new)['p']
        self.assertIsNone(empty['a2']['success_fraction_bounds'])
        self.assertIsNone(empty['a2_minus_original_fraction_bounds'])

    def test_initial_fingerprints_use_hash_not_reused_episode_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, digest in [('old', 'old-bytes'), ('new', 'new-bytes')]:
                folder = root / name / 'c000'
                folder.mkdir(parents=True)
                row = dict(case_id='c000', episode_id='c000/shared/000',
                           policy_id='shared', phase='initial', payload_sha256=digest)
                (folder / 'episodes.jsonl').write_text(json.dumps(row) + '\n')
            self.assertNotEqual(initial_payload_fingerprints(root / 'old'),
                                initial_payload_fingerprints(root / 'new'))
            path = root / 'old/c000/episodes.jsonl'
            path.write_text(path.read_text() * 2)
            with self.assertRaisesRegex(ValueError, 'Duplicate initial'):
                initial_payload_fingerprints(root / 'old')

    def test_usage_fields_are_not_added_and_failed_call_usage_stays_unknown(self):
        completed = dict(stage='attacker_p0', status='completed', prompt='a' * 10,
                         system_prompt='sys', provider=dict(seconds=2,
                         usage=dict(input_tokens=100, cache_read_input_tokens=80,
                                    cache_creation_input_tokens=0, output_tokens=30)))
        failed = dict(stage='attacker_p0', status='error', error_type='RuntimeError',
                      prompt='b' * 20, system_prompt='sys', provider=dict(seconds=900))
        row = generation_summary([dict(calls=[completed, failed])])['stages']['attacker_p0']
        self.assertEqual(row['calls'], 2)
        self.assertEqual(row['usage_missing_calls'], 1)
        self.assertEqual(row['usage']['input_tokens']['total'], 100)
        self.assertEqual(row['usage']['input_tokens']['count'], 1)
        self.assertEqual(row['usage']['cache_read_input_tokens']['total'], 80)
        self.assertEqual(row['total_request_chars']['total'], 36)
        self.assertEqual(row['seconds']['total'], 902)
        self.assertEqual(row['successful_call_seconds']['median'], 2)
        self.assertEqual(row['errors'], {'RuntimeError': 1})

    def test_prompt_reduction_uses_same_route_metadata_not_old_sample_lengths(self):
        generation = dict(case_id='c000', policy_id='shared', calls=[dict(
            stage='attacker_p0', prompt='a' * 140, system_prompt='s' * 10, status='completed')],
            routing=dict(a2=dict(original_document_chars=300, a2_document_chars=100,
                                 selections={'strategy': {'case_ids': ['case1', 'case2']}})))
        row = generation_summary([generation])['a2_initial_prompt_reduction'][0]
        self.assertEqual(row['actual_initial_request_chars'], 150)
        self.assertEqual(row['same_route_original_request_chars'], 350)
        self.assertAlmostEqual(row['strategy_reduction_fraction'], 2 / 3)
        self.assertEqual(row['selected_case_ids'], {'strategy': ['case1', 'case2']})

    def test_generation_error_keeps_unknown_outcome_and_planned_denominators(self):
        from scripts.analyze_observable_pilot5_20261029 import EXPECTED_IDS, ARMS
        from tests.test_observable_pilot_analysis import episode, decision, write_json, write_jsonl
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = dict(post_failure_target_budget=3, policies={pid: {} for pid in ARMS},
                          attacker=dict(mode='a2', model='fixture'))
            cases = [dict(case_id=cid, goal_spec=dict(trigger_ids=['receipt'])) for cid in EXPECTED_IDS]
            write_json(root / 'manifest.json', dict(config=config, cases=cases))
            write_json(root / 'status.json', dict(status='completed'))
            write_json(root / 'worker_results.json', [dict(case_id=cid, status='completed') for cid in EXPECTED_IDS])
            for cid in EXPECTED_IDS:
                folder = root / cid
                folder.mkdir()
                initial = episode(cid)
                episodes, decisions, outcomes = [initial], [], []
                for pid in ARMS:
                    branch = [initial]
                    failed = cid == 'c004' and pid == 'stage_aware'
                    count = 2 if failed else 3
                    actions = ['Revise'] * 3 if pid == 'stage_aware' else ['Retry', 'Revise', 'Retry']
                    for ordinal in range(1, 4):
                        decisions.append(decision(cid, pid, branch, actions[ordinal - 1], ordinal - 1))
                        if ordinal <= count:
                            payload = f'p{ordinal}' if pid == 'stage_aware' else ('p0' if ordinal == 1 else 'p1')
                            completed = episode(cid, pid, ordinal, False, 'F', payload)
                            branch.append(completed)
                            episodes.append(completed)
                    decisions.append(decision(cid, pid, branch, 'Stop', count,
                                               reason='GENERATION_ERROR' if failed else 'budget_exhausted'))
                    outcomes.append(dict(case_id=cid, policy_id=pid, initial_gold=False,
                                         gold_success=None if failed else False,
                                         status='GENERATION_ERROR' if failed else 'VALID_GOLD_FALSE',
                                         target_attempts=count, revisions=3 if pid == 'stage_aware' else 1))
                events = [dict(event='TARGET_STARTED', episode_id=e['episode_id']) for e in episodes]
                if cid == 'c004':
                    raw = folder / 'raw'
                    raw.mkdir()
                    generation = dict(case_id=cid, policy_id='stage_aware', generation_id=cid + '/stage_aware/P3',
                        status='GENERATION_ERROR', mode='a2', calls=[dict(stage='attacker_revise', status='error',
                        error_type='RuntimeError', prompt='synthetic', provider=dict(status='error',
                        error_type='RuntimeError', seconds=846.6680394769646))])
                    write_json(raw / 'generation.json', generation)
                    events.append(dict(event='GENERATION_COMPLETED', raw_path='raw/generation.json'))
                write_json(folder / 'manifest.json', dict(config=config))
                write_json(folder / 'status.json', dict(status='completed'))
                write_json(folder / 'outcomes.json', outcomes)
                write_json(folder / 'metrics.json', dict(physical_total_cost=dict(target_executions=len(episodes))))
                write_jsonl(folder / 'episodes.jsonl', episodes)
                write_jsonl(folder / 'decisions.jsonl', decisions)
                write_jsonl(folder / 'events.jsonl', events)
            report = summarize(root)
            group = report['policies']['stage_aware']
            self.assertEqual(report['analysis_status'], 'complete')
            self.assertEqual(group['assigned_cases'], 5)
            self.assertEqual(group['confirmed_successes'], 0)
            self.assertEqual(group['valid_failures'], 4)
            self.assertEqual(group['unknown_or_error_outcomes'], 1)
            self.assertIsNone(group['complete_success_fraction'])
            self.assertEqual(group['first_failure_cases'], 5)
            self.assertEqual(group['post_failure_unknown_outcomes'], 1)
            self.assertIsNone(group['complete_post_failure_success_fraction'])
            self.assertEqual(report['cost']['target_results_recorded'], 34)
            self.assertEqual(report['coverage']['trigger_counts'], {'T': 0, 'F': 34, 'U': 0})
            stage = report['generation']['stages']['attacker_revise']
            self.assertEqual(stage['errors'], {'RuntimeError': 1})
            self.assertEqual(stage['usage_missing_calls'], 1)
            self.assertEqual(stage['seconds']['total'], 846.6680394769646)
            # Until both arms finish, the Runner has not committed outcomes.json.
            # Existing false episodes must not be mistaken for terminal failures.
            (root / 'c004/outcomes.json').unlink()
            write_json(root / 'c004/status.json', dict(status='running'))
            interim = summarize(root)
            self.assertEqual(interim['analysis_status'], 'partial_or_audit_issues')
            self.assertEqual(interim['policies']['stage_aware']['valid_failures'], 4)
            self.assertEqual(interim['policies']['stage_aware']['unknown_or_error_outcomes'], 1)

    def test_only_committed_generation_records_count_and_duplicates_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'c000'
            raw = folder / 'raw'
            raw.mkdir(parents=True)
            completed = dict(case_id='c000', calls=[])
            (raw / 'done.json').write_text(json.dumps(completed))
            (raw / 'in_progress.json').write_text('{')
            event = dict(event='GENERATION_COMPLETED', raw_path='raw/done.json')
            events = folder / 'events.jsonl'
            events.write_text(json.dumps(event) + '\n')
            self.assertEqual(generation_records(directory), [completed])
            events.write_text((json.dumps(event) + '\n') * 2)
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                generation_records(directory)


if __name__ == '__main__':
    unittest.main()
