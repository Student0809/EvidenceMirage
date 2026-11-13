"""Budget, provenance and observation-denominator regression tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.evaluation.task_sampling import score_run
from src.evaluation.probe_sampling import score_run as legacy_score_run
from src.evidence.task_sampling import PROTOCOL, load_profiles, marker, public_packets
from src.evidence.probe_sampling import PROTOCOL as LEGACY_PROTOCOL
from src.mismatch.data import digest, write_json

ROOT = Path(__file__).resolve().parents[1]
PROFILES = load_profiles(ROOT / 'configs/mismatch/probe_sampling_all18_profiles.json')
NONCE = 'a' * 16


def freeze(folder):
    write_json(folder / 'frozen.json', dict(files={
        str(path.relative_to(folder)): digest(path)
        for path in folder.rglob('*.json') if path.name != 'frozen.json'}))


def make_run(directory, seeds, *, mode='probe', visibility='visible'):
    run = Path(directory) / 'run'
    write_json(run / 'manifest.json', dict(
        protocol=PROTOCOL, mode=mode, feedback_visibility=visibility,
        profiles=PROFILES, resumable=False, view='task_feedback',
        target_budget_per_branch=3))
    write_json(run / 'status.json', dict(status='predictions_frozen'))
    write_json(run / 'selection_private.json', [dict(
        branch=f'b{index:03d}', seed=seed, suite=PROFILES[seed]['suite'], repeat=0)
        for index, seed in enumerate(seeds)])
    for index, seed in enumerate(seeds):
        folder = run / 'branches' / f'b{index:03d}'
        write_json(folder / 'job_public.json', dict(
            protocol=PROTOCOL, profile=PROFILES[seed], budget=3, mode=mode,
            feedback_visibility=visibility))
        write_json(folder / 'branch.json', dict(status='budget_exhausted', target_attempts=1, decisions=[]))
    return run


def execution(run, index, seed, ordinal=1, *, gold=False, native=None,
              delivered=True, events=1, visibility='visible', final='done'):
    profile = PROFILES[seed]
    folder = run / 'branches' / f'b{index:03d}' / 'executions' / f'{ordinal:02d}'
    episode = f'b{index:03d}/{ordinal}'
    config = dict(profile, nonce=NONCE)
    write_json(folder / 'request_public.json', dict(
        case={}, payload=dict(id='P0', text='task injection'), episode_id=episode,
        target_config=dict(task_feedback=config)))
    report = dict(protocol=PROTOCOL, seed_id=seed, suite=profile['suite'], route=profile['route'],
                  receiver=profile['receiver'], episode_id=episode, nonce=NONCE, status='completed',
                  deliveries=[], events=[], final_text=final if seed == 'S07' else '', diagnostics={})
    if delivered and profile['route'] in ('email_report', 'slack_report'):
        delivery = dict(event_id='report_0', recipient=profile['receiver'],
                        body='PRIVATE report ' + marker(NONCE), accepted=True, reason=None)
        if profile['route'] == 'email_report':
            delivery['subject'] = 'status'
        report['deliveries'].append(delivery)
    elif profile['route'] == 'payment_events':
        report['events'] = [dict(event_id=f'payment_{number}', payload=dict(
            recipient=profile['receiver'], amount=10.0, subject='invoice', date='2026-11-13'))
            for number in range(events)]
    derived = seed in ('S13', 'S14', 'S16', 'S17')
    raw = dict(task_feedback=report, native_gold=None if derived else gold if native is None else native,
               gold_source='derived_goal' if derived else 'native', utility_source='native')
    write_json(folder / 'execution_private.json', dict(
        gold=gold, utility=True, raw=raw, error=None,
        usage=dict(api_calls=1, input_tokens=10, output_tokens=3)))
    write_json(folder / 'feedback_public.json', dict(
        final_output=final, error=None, packets=public_packets(report) if visibility == 'visible' else []))
    return folder


class TaskSamplingMetricsTests(unittest.TestCase):
    def score(self, run):
        for folder in (run / 'branches').glob('*'):
            freeze(folder)
        return score_run(run, run.parent / 'metrics.json')

    def test_success_counts_branches_and_false_stop_uses_latest_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            run = make_run(directory, ['S01', 'S13'])
            for ordinal, gold in enumerate((True, True, False), 1):
                execution(run, 0, 'S01', ordinal, gold=gold)
            write_json(run / 'branches/b000/branch.json', dict(
                status='self_reported_complete', target_attempts=3, decisions=[dict(action='Stop')]))
            execution(run, 1, 'S13', gold=True)
            metrics = self.score(run)
            self.assertEqual(metrics['attack_success_at_budget'], 2)
            self.assertEqual(metrics['attack_success_rate'], 1)
            self.assertEqual(metrics['execution_attack_success_count'], 3)
            self.assertEqual(metrics['execution_attack_success_rate'], 3 / 4)
            self.assertEqual(metrics['false_stop_count'], 1)
            self.assertEqual(metrics['false_stop_rate'], 1)
            self.assertEqual(metrics['native_attack_success_at_budget'], 1)
            self.assertEqual(metrics['native_scored_executions'], 3)
            derived = metrics['execution_rows'][-1]
            self.assertTrue(derived['task_gold'])
            self.assertIsNone(derived['native_gold'])
            self.assertEqual(derived['gold_source'], 'derived_goal')

    def test_route_denominators_events_and_unscored_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            seeds = ['S01', 'S04', 'S16', 'S07']
            run = make_run(directory, seeds)
            for index, seed in enumerate(seeds):
                execution(run, index, seed, delivered=index != 1, events=2)
            metrics = self.score(run)
            self.assertEqual(metrics['report_delivery_applicable_executions'], 2)
            self.assertEqual(metrics['report_delivered_executions'], 1)
            self.assertEqual(metrics['report_delivery_rate'], 0.5)
            self.assertEqual(metrics['payment_events'], 2)
            self.assertEqual(metrics['payment_event_visibility_rate'], 1)
            self.assertEqual(metrics['payment_field_coverage'], dict.fromkeys(
                ('recipient', 'amount', 'subject', 'date'), 1.0))
            self.assertEqual(metrics['final_report_present_count'], 1)
            self.assertIsNone(metrics['by_route']['payment_events']['report_delivery_rate'])
            self.assertIsNone(metrics['by_seed']['S07']['report_delivery_rate'])
            self.assertEqual(metrics['by_suite']['banking']['instances'], 2)
            for row in metrics['execution_rows']:
                self.assertIsNone(row['report_accuracy'])
                self.assertFalse(row['report_accuracy_scored'])
            self.assertIsNone(metrics['report_accuracy'])
            self.assertNotIn('PRIVATE', (run.parent / 'metrics.json').read_text())

    def test_all18_derived_gold_provenance_and_delivery_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            seeds = sorted(PROFILES)
            run = make_run(directory, seeds)
            for index, seed in enumerate(seeds):
                execution(run, index, seed, gold=True)
            metrics = self.score(run)
            self.assertEqual(metrics['instances'], 18)
            self.assertEqual(metrics['attack_success_at_budget'], 18)
            self.assertEqual(metrics['report_delivery_applicable_executions'], 14)
            self.assertEqual(metrics['payment_executions'], 3)
            self.assertEqual(metrics['final_report_executions'], 1)
            self.assertEqual(metrics['gold_source_counts'], {'native': 14, 'derived_goal': 4})
            self.assertEqual(metrics['native_attack_success_at_budget'], 14)
            self.assertEqual(set(metrics['by_seed']), set(PROFILES))

    def test_missing_branches_and_dispatched_executions_remain_in_denominators(self):
        with tempfile.TemporaryDirectory() as directory:
            run = make_run(directory, ['S01', 'S07', 'S13'])
            execution(run, 0, 'S01', gold=True)
            write_json(run / 'branches/b000/branch.json', dict(status='target_error', target_attempts=3))
            execution(run, 1, 'S07', gold=False, final='')
            for name in ('b000', 'b001'):
                freeze(run / 'branches' / name)
            metrics = score_run(run, run.parent / 'metrics.json')
            self.assertEqual(metrics['instances'], 3)
            self.assertEqual(metrics['missing_branches'], 1)
            self.assertEqual(metrics['attack_success_rate'], 1 / 3)
            self.assertEqual(metrics['executions'], 4)
            self.assertEqual(metrics['execution_attack_success_rate'], 1 / 4)
            self.assertEqual(metrics['missing_request_executions'], 2)
            self.assertEqual(metrics['task_gold_unknown_executions'], 2)
            self.assertEqual(metrics['report_delivery_rate'], 1 / 3)
            self.assertEqual(metrics['final_report_present_count'], 0)

    def test_hidden_no_probe_keeps_actual_events_without_public_visibility(self):
        with tempfile.TemporaryDirectory() as directory:
            run = make_run(directory, ['S16', 'S01'], mode='no_probe', visibility='hidden')
            execution(run, 0, 'S16', visibility='hidden')
            execution(run, 1, 'S01', visibility='hidden', delivered=False)
            metrics = self.score(run)
            self.assertEqual(metrics['payment_events'], 1)
            self.assertEqual(metrics['attacker_visible_payment_events'], 0)
            self.assertEqual(metrics['payment_event_observation_rate'], 1)
            self.assertEqual(metrics['payment_event_visibility_rate'], 0)
            self.assertEqual(metrics['public_projection_mismatches'], 0)
            self.assertEqual(metrics['report_requested_executions'], 0)

    def test_target_errors_are_unknown_and_remain_in_route_denominators(self):
        with tempfile.TemporaryDirectory() as directory:
            run = make_run(directory, ['S01'])
            folder = execution(run, 0, 'S01', delivered=False)
            write_json(folder / 'execution_private.json', dict(gold=None, raw={}, error='TimeoutError'))
            metrics = self.score(run)
            self.assertEqual(metrics['error_executions'], 1)
            self.assertEqual(metrics['task_gold_unknown_executions'], 1)
            self.assertEqual(metrics['report_delivery_applicable_executions'], 1)
            self.assertEqual(metrics['report_delivery_rate'], 0)
            self.assertEqual(metrics['execution_attack_success_rate'], 0)
            self.assertIsNone(metrics['execution_rows'][0]['native_gold'])
            self.assertIsNone(metrics['report_accuracy'])

    def test_public_projection_mismatch_and_cross_episode_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            run = make_run(directory, ['S01', 'S04'])
            first = execution(run, 0, 'S01')
            second = execution(run, 1, 'S04')
            public = json.loads((first / 'feedback_public.json').read_text())
            public['packets'] = []
            write_json(first / 'feedback_public.json', public)
            private = json.loads((second / 'execution_private.json').read_text())
            private['raw']['task_feedback']['episode_id'] = 'another/episode'
            write_json(second / 'execution_private.json', private)
            metrics = self.score(run)
            self.assertEqual(metrics['public_projection_mismatches'], 2)
            self.assertEqual(metrics['invalid_feedback_executions'], 1)
            self.assertEqual(metrics['report_delivered_executions'], 1)
            self.assertEqual(metrics['attacker_visible_reports'], 0)

    def test_success_after_budget_does_not_count_and_frozen_changes_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            run = make_run(directory, ['S01'])
            for ordinal in range(1, 5):
                execution(run, 0, 'S01', ordinal, gold=ordinal == 4)
            write_json(run / 'branches/b000/branch.json', dict(status='budget_exhausted', target_attempts=4))
            metrics = self.score(run)
            self.assertEqual(metrics['attack_success_at_budget'], 0)
            self.assertEqual(metrics['execution_attack_success_count'], 1)
            self.assertEqual(metrics['over_budget_executions'], 1)
            write_json(run / 'branches/b000/branch.json', dict(status='changed'))
            with self.assertRaisesRegex(ValueError, 'Frozen sampling artifact changed'):
                score_run(run, run.parent / 'changed_metrics.json')

    def test_frozen_profile_registry_and_parent_budget_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            run = make_run(directory, ['S01'])
            execution(run, 0, 'S01')
            freeze(run / 'branches/b000')
            write_json(run / 'profiles.json', dict(protocol=PROTOCOL, profiles=PROFILES))
            manifest = json.loads((run / 'manifest.json').read_text())
            manifest['profiles_sha256'] = digest(run / 'profiles.json')
            write_json(run / 'manifest.json', manifest)
            write_json(run / 'profiles.json', {})
            with self.assertRaisesRegex(ValueError, 'profile registry changed'):
                score_run(run, run.parent / 'metrics.json')
            write_json(run / 'profiles.json', dict(protocol=PROTOCOL, profiles=PROFILES))
            manifest['max_target_dispatches'] = 6
            write_json(run / 'manifest.json', manifest)
            with self.assertRaisesRegex(ValueError, 'Parent manifest budget'):
                score_run(run, run.parent / 'metrics.json')
            manifest['max_target_dispatches'] = 3
            write_json(run / 'manifest.json', manifest)
            job = json.loads((run / 'branches/b000/job_public.json').read_text())
            job['budget'] = 4
            write_json(run / 'branches/b000/job_public.json', job)
            freeze(run / 'branches/b000')
            with self.assertRaisesRegex(ValueError, 'Branch Target budget'):
                score_run(run, run.parent / 'metrics.json')

    def test_real_prepared_campaign_four_routes_and_hidden_control(self):
        from src.mismatch.online import target_job
        from src.mismatch.sampling import run_sampling_branch
        from src.mismatch.task_sampling import prepare_campaign
        from src.mismatch.tasks import MismatchTargetAdapter
        from tests.test_task_sampling_runtime import TaskRuntimeTests, ScriptedAttacker, pipeline

        def native_adapter(**kwargs):
            return MismatchTargetAdapter(pipeline=pipeline(kwargs['task_feedback']), **kwargs)

        def dispatch(command, **kwargs):
            with patch('src.mismatch.online.configure'), patch(
                    'src.mismatch.tasks.MismatchTargetAdapter', side_effect=native_adapter):
                target_job(command[-1])

        with tempfile.TemporaryDirectory() as directory:
            for mode in ('probe', 'no_probe'):
                with self.subTest(mode=mode):
                    run = Path(directory) / mode
                    _, jobs = prepare_campaign(TaskRuntimeTests().args(run, mode))
                    for index in (0, 3, 5, 6):
                        with patch('src.mismatch.sampling.configure'), patch(
                                'src.mismatch.sampling.PIMinerAdapter', ScriptedAttacker), patch(
                                'src.mismatch.sampling.subprocess.run', side_effect=dispatch):
                            result = run_sampling_branch(jobs[index])
                        self.assertEqual(result['status'], 'self_reported_complete', result)
                    write_json(run / 'status.json', dict(status='predictions_frozen'))
                    metrics = score_run(run, Path(directory) / f'{mode}-metrics.json')
                    self.assertEqual(metrics['instances'], 18)
                    self.assertEqual(metrics['missing_branches'], 14)
                    self.assertEqual(metrics['executions'], 12)
                    self.assertEqual(metrics['report_delivery_applicable_executions'], 6)
                    self.assertEqual(metrics['report_delivered_executions'], 6)
                    self.assertEqual(metrics['payment_events'], 3)
                    self.assertEqual(metrics['final_report_present_count'], 3)
                    self.assertEqual(metrics['invalid_feedback_executions'], 0)
                    self.assertEqual(metrics['public_projection_mismatches'], 0)
                    self.assertEqual(metrics['attacker_visible_reports'], 6 if mode == 'probe' else 0)
                    self.assertEqual(metrics['attacker_visible_payment_events'], 3 if mode == 'probe' else 0)


class LegacySamplingMetricRegressionTests(unittest.TestCase):
    def test_legacy_branch_success_is_not_execution_success_and_derived_is_labelled(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / 'run'
            branch = run / 'branches/b000'
            write_json(run / 'manifest.json', dict(protocol=LEGACY_PROTOCOL,
                mode='no_probe', resumable=False, target_budget_per_branch=3))
            write_json(run / 'status.json', dict(status='predictions_frozen'))
            write_json(branch / 'job_public.json', dict(mode='no_probe', budget=3))
            write_json(branch / 'branch.json', dict(status='budget_exhausted', target_attempts=3))
            for ordinal in range(1, 4):
                folder = branch / 'executions' / f'{ordinal:02d}'
                write_json(folder / 'request_public.json', dict(case={}, payload=dict(id='P0', text='task')))
                raw = dict(native_gold=None, gold_source='derived_goal') if ordinal == 3 else {}
                write_json(folder / 'execution_private.json', dict(gold=True, raw=raw))
                write_json(folder / 'feedback_public.json', dict(packets=[]))
            freeze(branch)
            metrics = legacy_score_run(run, run.parent / 'metrics.json')
            self.assertEqual(metrics['attack_success_at_budget'], 1)
            self.assertEqual(metrics['attack_success_rate'], 1)
            self.assertEqual(metrics['execution_attack_success_count'], 3)
            self.assertEqual(metrics['native_scored_execution_count'], 2)
            self.assertIsNone(metrics['execution_rows'][-1]['native_gold'])
            self.assertEqual(metrics['execution_rows'][-1]['gold_source'], 'derived_goal')


if __name__ == '__main__':
    unittest.main()
