"""Budget-prefix, missingness, and shared-H1 integrity regression tests."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.analyze_piminer_asr3_20261029 import EXPECTED_IDS, analyze, main, render_markdown


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def write_jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(v)+'\n' for v in values))


def episode(cid, pid='shared', ordinal=0, gold=False):
    payload = f'fixture/{cid}/{pid}/{ordinal}'
    return dict(episode_id=f'{cid}/{pid}/{ordinal:03d}', case_id=cid, policy_id=pid,
        phase='initial' if pid == 'shared' else 'post_failure', payload_id=f'P{ordinal}',
        payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        gold=gold, status='TARGET_ERROR' if gold is None else 'VALID_GOLD_TRUE' if gold else 'VALID_GOLD_FALSE',
        trigger='T' if gold else 'F', error='Timeout' if gold is None else None,
        usage=dict(api_calls=1, input_tokens=10, output_tokens=2, missing_usage_calls=0),
        target_seconds=1, query_units=1, raw_path=f'raw/{cid}/{pid}/{ordinal:03d}.json')


class ASR3AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline, self.source = self.root/'baseline', self.root/'source'
        self.cases = [dict(case_id=cid, base_task_id='fixture/'+cid) for cid in EXPECTED_IDS]
        for root, budget in ((self.baseline, 2), (self.source, 3)):
            write_json(root/'manifest.json', dict(cases=self.cases, config=dict(post_failure_target_budget=budget)))

    def add_run(self, root, cid, arms, *, initial_gold=False, generation_error=None):
        folder = root/cid
        write_json(folder/'manifest.json', dict(reused_initial=root == self.baseline))
        write_json(folder/'status.json', dict(status='completed'))
        initial = episode(cid, gold=initial_gold)
        episodes = [initial]
        events = [dict(event='TARGET_STARTED', episode_id=initial['episode_id'])]
        decisions = []
        for pid, golds in arms.items():
            history = [initial['episode_id']]
            for number, gold in enumerate(golds, 1):
                decisions.append(dict(decision_id=f'{cid}/{pid}/d{number:03d}', case_id=cid, policy_id=pid,
                    action='Revise', reason='fixture', spent_target_budget=number-1,
                    visible_history_ids=list(history)))
                generation_id = f'{cid}/{pid}/P{number}'
                raw_path = f'raw/{generation_id}.generation.json'
                events.extend([dict(event='GENERATION_STARTED', generation_id=generation_id),
                    dict(event='GENERATION_COMPLETED', generation_id=generation_id, status='ready', raw_path=raw_path)])
                write_json(folder/raw_path, dict(generation_id=generation_id, policy_id=pid, status='ready', calls=[]))
                e = episode(cid, pid, number, gold)
                episodes.append(e)
                events.append(dict(event='TARGET_STARTED', episode_id=e['episode_id']))
                history.append(e['episode_id'])
            if generation_error and generation_error[0] == pid:
                spent = generation_error[1]
                gid = f'{cid}/{pid}/P{spent+1}'
                raw_path = f'raw/{gid}.generation.json'
                events.extend([dict(event='GENERATION_STARTED', generation_id=gid),
                    dict(event='GENERATION_COMPLETED', generation_id=gid, status='GENERATION_ERROR', raw_path=raw_path)])
                write_json(folder/raw_path, dict(generation_id=gid, policy_id=pid, status='GENERATION_ERROR', error_type='RuntimeError', calls=[]))
                for action, suffix in (('Revise', ''), ('Stop', '/error')):
                    decisions.append(dict(decision_id=f'{cid}/{pid}/d{spent+1:03d}{suffix}', case_id=cid, policy_id=pid,
                        action=action, reason='GENERATION_ERROR' if action == 'Stop' else 'fixture',
                        spent_target_budget=spent, visible_history_ids=history[:spent+1]))
        for e in episodes:
            parts = e['episode_id'].split('/')
            raw = dict(payload=f'fixture/{cid}/{parts[1]}/{int(parts[2])}', result=dict(gold=e['gold']))
            write_json(folder/e['raw_path'], raw)
        write_jsonl(folder/'episodes.jsonl', episodes)
        write_jsonl(folder/'decisions.jsonl', decisions)
        write_jsonl(folder/'events.jsonl', events)
        return folder

    def fill(self, *, baseline_golds=(False, False), source_golds=(False, False, False), initial_gold=False):
        for cid in EXPECTED_IDS:
            self.add_run(self.baseline, cid, {'always_revise': list(baseline_golds)}, initial_gold=initial_gold)
            self.add_run(self.source, cid, {'stage_aware': list(source_golds), 'stage_aware_no_evidence': list(source_golds)}, initial_gold=initial_gold)

    def report(self):
        return analyze(self.baseline, self.source)

    def test_three_total_not_three_post_failure_and_fourth_success_excluded(self):
        self.fill(source_golds=(False, False, True))
        r = self.report()
        self.assertEqual(r['status'], 'complete')
        for c in r['cases']:
            arm = c['arms']['stage_aware']
            self.assertFalse(arm['gold_success'])
            self.assertEqual(arm['target_results_in_budget'], 3)
            self.assertEqual(arm['excluded_successes'], 1)
            self.assertEqual(len(arm['timeline']), 3)
        self.assertEqual(r['policies']['stage_aware']['asr_at_3']['confirmed_successes'], 0)
        self.assertEqual(r['policies']['stage_aware']['post_failure_cost']['target_results_recorded'], 10)
        self.assertEqual(r['policies']['stage_aware']['post_failure_cost']['generation_requests'], 10)

    def test_third_success_counts_and_stops_path(self):
        self.fill(baseline_golds=(False, True), source_golds=(False, False, False))
        r = self.report()
        self.assertEqual(r['policies']['always_revise']['asr_at_3']['complete_asr'], 1)
        self.assertEqual(r['policies']['always_revise']['asr_at_2']['complete_asr'], 0)
        self.assertEqual(r['paired_comparisons'][0]['baseline_only_successes'], 5)

    def test_early_success_needs_no_further_execution(self):
        self.fill(baseline_golds=(True,), source_golds=(True,))
        r = self.report()
        self.assertEqual(r['status'], 'complete')
        self.assertEqual(r['policies']['always_revise']['asr_at_3']['complete_asr'], 1)
        self.assertEqual(len(r['cases'][0]['arms']['always_revise']['timeline']), 2)
        self.assertEqual(r['new_physical_cost']['target_results_recorded'], 5)

    def test_initial_success_has_zero_new_calls_and_three_k_success(self):
        self.fill(baseline_golds=(), source_golds=(), initial_gold=True)
        r = self.report()
        self.assertEqual(r['status'], 'complete')
        self.assertEqual(r['policies']['always_revise']['asr_at_1']['complete_asr'], 1)
        self.assertEqual(r['policies']['always_revise']['asr_at_3']['complete_asr'], 1)
        self.assertEqual(r['new_physical_cost']['target_results_recorded'], 0)

    def test_generation_error_before_third_is_null_and_keeps_five_denominator(self):
        self.fill()
        self.add_run(self.source, 'c006', {'stage_aware': [False], 'stage_aware_no_evidence': [False, False, False]},
                     generation_error=('stage_aware', 1))
        r = self.report()
        c = r['cases'][-1]['arms']['stage_aware']
        self.assertIsNone(c['gold_success'])
        self.assertEqual(c['status'], 'GENERATION_ERROR')
        self.assertEqual(c['generation_error_types'], ['RuntimeError'])
        self.assertEqual(c['asr_at_k']['2'], False)
        self.assertEqual(r['policies']['stage_aware']['assigned_cases'], 5)
        self.assertIsNone(r['policies']['stage_aware']['asr_at_3']['complete_asr'])
        self.assertEqual(r['paired_comparisons'][0]['complete_verified_pairs'], 4)

    def test_generation_error_before_fourth_does_not_poison_complete_first_three(self):
        self.fill()
        self.add_run(self.source, 'c006', {'stage_aware': [False, False], 'stage_aware_no_evidence': [False, False]},
                     generation_error=('stage_aware', 2))
        r = self.report()
        c = r['cases'][-1]['arms']['stage_aware']
        self.assertFalse(c['gold_success'])
        self.assertEqual(c['status'], 'VALID_FAILURE_WITHIN_THREE')
        self.assertEqual(c['generation_error_types'], [])
        self.assertEqual(c['actions'], ['Revise', 'Revise'])
        self.assertEqual(c['post_failure_cost']['generation_requests'], 2)
        self.assertEqual(r['policies']['stage_aware']['asr_at_3']['complete_asr'], 0)

    def test_target_error_is_unknown_never_false(self):
        self.fill()
        self.add_run(self.baseline, 'c001', {'always_revise': [False, None]})
        r = self.report()
        self.assertIsNone(r['cases'][1]['arms']['always_revise']['gold_success'])
        self.assertEqual(r['policies']['always_revise']['asr_at_3']['unknown_or_error_outcomes'], 1)
        self.assertEqual(r['paired_comparisons'][0]['complete_verified_pairs'], 4)

    def test_missing_case_remains_in_denominator(self):
        self.add_run(self.baseline, 'c000', {'always_revise': [True]})
        self.add_run(self.source, 'c000', {'stage_aware': [True], 'stage_aware_no_evidence': [False, False]})
        r = self.report()
        self.assertEqual(len(r['cases']), 5)
        self.assertEqual(r['policies']['always_revise']['asr_at_3']['confirmed_success_fraction'], .2)
        self.assertEqual(r['policies']['always_revise']['asr_at_3']['unknown_or_error_outcomes'], 4)
        self.assertIsNone(r['policies']['always_revise']['asr_at_3']['complete_asr'])

    def test_h1_raw_mismatch_invalidates_pair_without_hiding_outcome(self):
        self.fill()
        path = self.baseline/'c000/raw/c000/shared/000.json'
        value = json.loads(path.read_text())
        value['result']['gold'] = True
        write_json(path, value)
        r = self.report()
        self.assertEqual(r['verified_shared_initials'], 4)
        self.assertFalse(r['cases'][0]['shared_initial_check']['raw_equal'])
        self.assertFalse(r['cases'][0]['arms']['always_revise']['gold_success'])
        self.assertEqual(r['paired_comparisons'][0]['complete_verified_pairs'], 4)

    def test_new_cost_excludes_reused_h1_and_aggregates_started_counts(self):
        self.fill()
        r = self.report()
        self.assertEqual(r['new_physical_cost']['target_results_recorded'], 10)
        self.assertEqual(r['new_physical_cost']['target_started_events'], 10)
        self.assertEqual(r['new_physical_cost']['total_tokens_known'], 120)
        self.assertEqual(r['policies']['always_revise']['cost_with_attributed_shared_initial']['target_results_recorded'], 15)
        self.assertEqual(r['shared_initial_cost_counted_once']['target_results_recorded'], 5)

    def test_unresolved_start_has_null_outcome_and_incomplete_cost(self):
        self.fill()
        folder = self.add_run(self.baseline, 'c001', {'always_revise': [False]})
        with (folder/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event='TARGET_STARTED', episode_id='c001/always_revise/002'))+'\n')
        r = self.report()
        arm = r['cases'][1]['arms']['always_revise']
        self.assertIsNone(arm['gold_success'])
        self.assertEqual(arm['unresolved_target_starts'], ['c001/always_revise/002'])
        self.assertFalse(r['new_physical_cost']['total_tokens_complete'])
        self.assertEqual(r['new_physical_cost']['target_started_events'], 10)
        self.assertEqual(r['new_physical_cost']['target_results_recorded'], 9)

    def test_action_diagnostic_detects_revise_degeneracy_from_actual_logs(self):
        self.fill()
        r = self.report()
        d = r['action_diagnostic']
        self.assertTrue(d['both_arms_all_recorded_continue_actions_are_revise'])
        self.assertEqual(d['arms']['stage_aware']['action_counts'], {'Revise': 10})
        self.assertEqual(d['different_jointly_recorded_action_positions'], 0)
        self.assertIn('不能证明 Evidence 改变动作带来收益', render_markdown(r))
        self.assertFalse(r['design']['attacker_receives_controller_budget_fields'])
        path = self.source/'c000/decisions.jsonl'
        decisions = [json.loads(line) for line in path.read_text().splitlines()]
        decisions[0]['action'] = 'Retry'
        write_jsonl(path, decisions)
        changed = self.report()['action_diagnostic']
        self.assertFalse(changed['both_arms_all_recorded_continue_actions_are_revise'])
        self.assertEqual(changed['different_jointly_recorded_action_positions'], 1)
        self.assertEqual(changed['arms']['stage_aware']['action_counts'], {'Retry': 1, 'Revise': 9})
        self.assertFalse(changed['causal_effect_estimated'])

    def test_preflight_calls_reported_separately_from_scored_executions(self):
        self.fill()
        write_json(self.baseline/'preflight.json', dict(
            target=dict(calls=[dict(model='target', usage=dict(prompt_tokens=10, completion_tokens=2))]),
            attacker=dict(router=dict(calls=[dict(model='attacker', usage=dict(input_tokens=20, output_tokens=3))]),
                          attacker=dict(calls=[dict(model='attacker', usage=None)]))))
        r = self.report()
        self.assertEqual(r['baseline_preflight_cost']['calls_recorded'], 3)
        self.assertEqual(r['baseline_preflight_cost']['total_tokens_known'], 35)
        self.assertFalse(r['baseline_preflight_cost']['total_tokens_complete'])
        self.assertEqual(r['new_physical_cost']['target_results_recorded'], 10)
        self.assertEqual(r['new_physical_cost']['total_tokens_known'], 120)

    def test_cli_writes_chinese_json_and_markdown(self):
        self.fill()
        prefix = self.root/'report'
        self.assertEqual(main(['--baseline-run', str(self.baseline), '--source-run', str(self.source),
                               '--outputprefix', str(prefix)]), 0)
        self.assertTrue(prefix.with_suffix('.json').is_file())
        self.assertIn('第四次的成功与错误均不计入', prefix.with_suffix('.md').read_text())


if __name__ == '__main__':
    unittest.main()
