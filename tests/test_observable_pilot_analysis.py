"""Semantic checks for the offline five-case report, including missingness and shadow scope."""
import json
from pathlib import Path
import tempfile
import unittest
from scripts.analyze_observable_pilot5_20261029 import (
    EXPECTED_IDS, analyze, main, make_history, mask_evidence, shadow_decisions)
from src.policies.stage_aware import StageAware
from src.policies.stage_aware_no_evidence import StageAwareNoEvidence


def episode(cid, pid='shared', ordinal=0, gold=False, trigger='F', payload='p0'):
    stage = 'ERROR' if gold is None else 'SUCCESS' if gold else {'F': 'NOT_TRIGGERED', 'T': 'TRIGGERED_FAIL', 'U': 'UNOBSERVABLE'}[trigger]
    return dict(episode_id=f'{cid}/{pid}/{ordinal:03d}', case_id=cid, policy_id=pid,
                phase='initial' if pid == 'shared' else 'post_failure', gold=gold,
                trigger=trigger, stage=stage, payload_sha256=payload, payload_id=payload,
                response_signal='Partial', status='TARGET_ERROR' if gold is None else 'VALID_GOLD_TRUE' if gold else 'VALID_GOLD_FALSE',
                error='Timeout' if gold is None else None, observation_error=None,
                evidence=[dict(id='receipt', query='email_received', status=trigger, is_trigger=True)],
                usage=None, target_seconds=1, query_units=1, raw_path='raw/example.json')


def decision(cid, pid, es, action, spent=0, reason='test'):
    return dict(decision_id=f'{cid}/{pid}/d{spent+1:03d}', case_id=cid, policy_id=pid,
                visible_history_ids=[e['episode_id'] for e in es], action=action,
                spent_target_budget=spent, remaining_target_budget=3-spent, reason=reason)


def write_json(path, value):
    path.write_text(json.dumps(value))


def write_jsonl(path, values):
    path.write_text(''.join(json.dumps(v) + '\n' for v in values))


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dict(post_failure_target_budget=3, policies=dict(stage_aware=dict(
            max_retries_per_payload=2, unknown_schedule=['Retry', 'Revise', 'Retry']),
            stage_aware_no_evidence=dict(max_retries_per_payload=2, unknown_schedule=['Retry', 'Revise', 'Retry'])))
        self.cases = [dict(case_id=cid, base_task_id='workspace/'+cid,
                          injection_task_goal='send an email', goal_spec=dict(trigger_ids=['receipt']))
                      for cid in EXPECTED_IDS]
        write_json(self.root/'manifest.json', dict(cases=self.cases, config=self.config))
        write_json(self.root/'status.json', dict(status='completed'))
        write_json(self.root/'worker_results.json', [dict(case_id=cid, status='completed') for cid in EXPECTED_IDS])

    def add_case(self, cid, *, stage_error=False):
        folder = self.root/cid
        folder.mkdir()
        first = episode(cid)
        success = episode(cid, 'stage_aware', 1, None if stage_error else True, 'U' if stage_error else 'T', 'p1')
        fixed = episode(cid, 'stage_aware_no_evidence', 1, True, 'T')
        es = [first, success, fixed]
        ds = [decision(cid, 'stage_aware', [first], 'Revise'),
              decision(cid, 'stage_aware', [first, success], 'Stop', 1),
              decision(cid, 'stage_aware_no_evidence', [first], 'Retry'),
              decision(cid, 'stage_aware_no_evidence', [first, fixed], 'Stop', 1)]
        outcomes = [dict(case_id=cid, policy_id=pid, gold_success=None if stage_error and pid == 'stage_aware' else True,
                         status='TARGET_ERROR' if stage_error and pid == 'stage_aware' else 'VALID_GOLD_TRUE',
                         target_attempts=1, revisions=1 if pid == 'stage_aware' else 0)
                    for pid in ('stage_aware', 'stage_aware_no_evidence')]
        write_json(folder/'manifest.json', dict(config=self.config))
        write_json(folder/'status.json', dict(status='completed'))
        write_json(folder/'outcomes.json', outcomes)
        write_json(folder/'metrics.json', dict(physical_total_cost=dict(target_executions=3)))
        write_jsonl(folder/'episodes.jsonl', es)
        write_jsonl(folder/'decisions.jsonl', ds)
        write_jsonl(folder/'events.jsonl', [dict(event='TARGET_STARTED', episode_id=e['episode_id']) for e in es])
        return folder

    def test_mask_removes_evidence_but_preserves_success_and_error(self):
        first = episode('c000', trigger='T')
        current = episode('c000', 'stage_aware', 1, trigger='F')
        h = make_history(decision('c000', 'stage_aware', [first, current], 'Revise', 1),
                         {e['episode_id']: e for e in (first, current)})
        masked = mask_evidence(h)
        self.assertEqual([e.trigger for e in masked.completed], ['U', 'U'])
        self.assertTrue(all(e.stage == 'UNOBSERVABLE' for e in masked.completed))
        self.assertTrue(all(e.evidence[0].status == 'U' for e in masked.completed))
        self.assertEqual(masked.completed[0].response_signal, 'Partial')
        self.assertEqual(h.completed[0].trigger, 'T')
        for gold, stage in ((True, 'SUCCESS'), (None, 'ERROR')):
            first = episode('c000', gold=gold)
            h = make_history(decision('c000', 'stage_aware', [first], 'Stop'), {first['episode_id']: first})
            self.assertEqual(mask_evidence(h).last.stage, stage)

    def test_shadow_uses_same_history_and_has_no_reward(self):
        first = episode('c000', trigger='F')
        d = decision('c000', 'stage_aware', [first], 'Revise')
        issues = []
        rows, checks = shadow_decisions([d], {first['episode_id']: first}, StageAware(), issues)
        self.assertFalse(issues)
        self.assertFalse(checks)
        self.assertEqual(rows[0]['masked_stage_aware_action'], 'Retry')
        self.assertEqual(rows[0]['label_based_action'], 'Retry')
        self.assertTrue(rows[0]['action_changed_by_evidence'])
        self.assertEqual(rows[0]['source_episode_ids'], [first['episode_id']])
        self.assertIsNone(rows[0]['counterfactual_reward'])

    def test_fixed_schedule_cap_mismatch_is_not_hidden(self):
        # A revision that repeats identical bytes does not reset StageAware's payload cap.
        es = [episode('c000', trigger='T'), episode('c000', 'fixed_schedule', 1, trigger='T'),
              episode('c000', 'fixed_schedule', 2, trigger='T')]
        d = decision('c000', 'fixed_schedule', es, 'Retry', 2)
        issues = []
        _, checks = shadow_decisions([d], {e['episode_id']: e for e in es}, StageAware(), issues)
        self.assertFalse(checks[0]['matches_masked_stage_aware'])
        self.assertEqual(checks[0]['masked_stage_aware_action'], 'Revise')
        self.assertEqual(issues[0]['code'], 'baseline_masked_mismatch')

    def test_strict_masked_baseline_preserves_identical_payload_retry_cap(self):
        es = [episode('c000', trigger='T'), episode('c000', 'stage_aware_no_evidence', 1, trigger='F'),
              episode('c000', 'stage_aware_no_evidence', 2, trigger='T')]
        d = decision('c000', 'stage_aware_no_evidence', es, 'Revise', 2)
        history = make_history(d, {e['episode_id']: e for e in es})
        self.assertEqual(StageAwareNoEvidence().decide(history).action.value, 'Revise')
        issues = []
        _, checks = shadow_decisions([d], {e['episode_id']: e for e in es}, StageAware(), issues)
        self.assertTrue(checks[0]['matches_masked_stage_aware'])
        self.assertFalse(issues)

    def test_worker_process_failure_is_preserved(self):
        for cid in EXPECTED_IDS:
            self.add_case(cid)
        write_json(self.root/'worker_results.json', [dict(case_id=cid,
            status='worker_error' if cid == 'c004' else 'completed',
            **({'error_type': 'BrokenProcessPool'} if cid == 'c004' else {})) for cid in EXPECTED_IDS])
        write_json(self.root/'status.json', dict(status='completed_with_errors'))
        report = analyze(self.root)
        case = next(r for r in report['cases'] if r['case_id'] == 'c004')
        self.assertEqual(report['status'], 'partial_or_audit_issues')
        self.assertEqual(case['worker_result']['error_type'], 'BrokenProcessPool')
        self.assertTrue(any(i['code'] == 'worker_not_completed' for i in case['issues']))
        self.assertEqual(report['policies']['stage_aware']['assigned_cases'], 5)

    def test_missing_cases_keep_denominator_and_unknown_outcomes(self):
        self.add_case('c000')
        report = analyze(self.root)
        self.assertEqual(len(report['cases']), 5)
        self.assertEqual(report['status'], 'partial_or_audit_issues')
        group = report['policies']['stage_aware']
        self.assertEqual(group['assigned_cases'], 5)
        self.assertEqual(group['confirmed_successes'], 1)
        self.assertEqual(group['unknown_or_error_outcomes'], 4)
        self.assertEqual(group['valid_failures'], 0)
        self.assertIsNone(group['complete_success_fraction'])
        self.assertEqual(report['outcome_comparison']['incomplete_pairs'], 4)

    def test_complete_run_covers_shared_initial_once_and_writes_both_reports(self):
        for cid in EXPECTED_IDS:
            self.add_case(cid)
        report = analyze(self.root)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['coverage']['valid_episodes'], 15)
        self.assertEqual(report['decision_effect']['evidence_changes_action'], 5)
        self.assertEqual(report['decision_effect']['baseline_masked_mismatches'], 0)
        self.assertEqual(report['decision_effect']['baseline_masked_checks'], 10)
        self.assertEqual(report['outcome_comparison']['complete_pairs'], 5)
        self.assertEqual(report['physical_cost']['target_results_recorded'], 15)
        output = self.root/'report.json'
        self.assertEqual(main(['--run', str(self.root), '--output', str(output)]), 0)
        self.assertTrue(output.is_file())
        self.assertIn('没有反事实收益', output.with_suffix('.md').read_text())

    def test_target_error_keeps_null_gold_and_pair_missingness(self):
        for cid in EXPECTED_IDS:
            self.add_case(cid, stage_error=(cid == 'c004'))
        report = analyze(self.root)
        case = next(r for r in report['cases'] if r['case_id'] == 'c004')
        self.assertIsNone(case['arms']['stage_aware']['gold_success'])
        self.assertEqual(case['arms']['stage_aware']['gold_sequence'], [False, None])
        self.assertEqual(report['outcome_comparison']['incomplete_pairs'], 1)
        self.assertEqual(report['policies']['stage_aware']['unknown_or_error_outcomes'], 1)
        self.assertIsNone(report['policies']['stage_aware']['complete_post_failure_success_fraction'])
        self.assertEqual(report['coverage']['invalid_episodes'], 1)

    def test_unfinished_target_and_truncated_json_are_explicit(self):
        folder = self.add_case('c000')
        with (folder/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event='TARGET_STARTED', episode_id='c000/stage_aware/002')) + '\n')
        with (folder/'episodes.jsonl').open('a') as stream:
            stream.write('{"episode_id":')
        report = analyze(self.root)
        case = report['cases'][0]
        self.assertEqual(case['unresolved_target_starts'], ['c000/stage_aware/002'])
        self.assertTrue(any(i['code'] == 'invalid_jsonl_record' for i in case['issues']))
        self.assertEqual(len(report['cases']), 5)


if __name__ == '__main__':
    unittest.main()
