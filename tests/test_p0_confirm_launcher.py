"""Single-policy launch boundaries and offline reporting for P0 confirmation."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_evidence_parallel as launcher
from scripts.summarize_full_evidence import analyze, render_markdown
from src.adapters.fixture import FixtureAttacker
from src.engine.config import load_config, load_cases
from src.engine.storage import write_json
from src.evidence.observer import policy_for_case
from src.schema import TargetResult
from src.vendor.attacker_observer import Collector


POLICY = 'stage_aware_p0_confirm'


class FreshFixtureAttacker(FixtureAttacker):
    use_cached_p0 = False


class UntriggeredFailure:
    def execute(self, case, payload, episode_id):
        import agentdojo.default_suites
        from agentdojo.task_suite.load_suites import get_suite
        collector = Collector.for_native_rollout(policy_for_case(case),
            benchmark_version='v1.2.2', suite_name=case['suite_name'])
        collector.wrap_tools(get_suite('v1.2.2', case['suite_name']).tools)
        return TargetResult(False, final='Synthetic public feedback.',
                            observation=collector.finish('Synthetic public feedback.', rollout_complete=True))


class P0ConfirmLauncherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = load_config(launcher.ROOT / 'experiments/evidence_repeat_asr3_pilot5_20261030.yaml')
        self.config['post_failure_target_budget'] = 4
        self.case = load_cases(self.config)[0]

    def manifest(self, policy=POLICY):
        config = deepcopy(self.config)
        config['policies'] = {policy: dict(max_retries_per_payload=2)}
        return dict(config=config, cases=[self.case], case_ids=[self.case['case_id']],
                    inputs_sha256={}, environment={})

    def test_each_supported_single_policy_accepts_its_own_reference(self):
        for policy in ('stage_aware', POLICY):
            with self.subTest(policy=policy):
                manifest = self.manifest(policy)
                reference = self.root / policy
                write_json(reference/'manifest.json', manifest)
                with patch.object(launcher, 'check_frozen', return_value=[self.case]):
                    cases, comparison = launcher.validate_protocol(manifest, reference)
                self.assertEqual(cases, [self.case])
                self.assertTrue(comparison['controller_settings_equal'])
                self.assertEqual(comparison['policy_id'], policy)

    def test_different_policy_ids_cannot_be_claimed_same_controller(self):
        for current, old in ((POLICY, 'stage_aware'), ('stage_aware', POLICY)):
            with self.subTest(current=current):
                reference = self.root / ('reference_' + old)
                write_json(reference/'manifest.json', self.manifest(old))
                with patch.object(launcher, 'check_frozen', return_value=[self.case]), \
                        self.assertRaisesRegex(ValueError, 'Reference Controller differs'):
                    launcher.validate_protocol(self.manifest(current), reference)

    def test_same_policy_different_parameters_cannot_be_claimed_same_controller(self):
        manifest = self.manifest()
        reference = deepcopy(manifest)
        reference['config']['policies'][POLICY]['max_retries_per_payload'] = 3
        write_json(self.root/'reference/manifest.json', reference)
        with patch.object(launcher, 'check_frozen', return_value=[self.case]), \
                self.assertRaisesRegex(ValueError, 'Reference Controller differs'):
            launcher.validate_protocol(manifest, self.root/'reference')

    def test_multiple_unknown_or_heldout_policies_are_not_enabled(self):
        bad = [dict(stage_aware={}, stage_aware_p0_confirm={}), dict(always_retry={}), {}]
        for policies in bad:
            with self.subTest(policies=policies):
                manifest = self.manifest()
                manifest['config']['policies'] = policies
                with patch.object(launcher, 'check_frozen', return_value=[self.case]), self.assertRaises(ValueError):
                    launcher.validate_protocol(manifest)
                write_json(self.root/'bad/manifest.json', manifest)
                with self.assertRaises(ValueError):
                    analyze(self.root/'bad')
        manifest = self.manifest()
        manifest['config']['split'] = 'test'
        with patch.object(launcher, 'check_frozen', return_value=[self.case]), self.assertRaises(ValueError):
            launcher.validate_protocol(manifest)

    def run_fixture(self):
        manifest = self.manifest()
        run = self.root/'run'
        write_json(run/'manifest.json', manifest)
        write_json(run/'status.json', dict(status='completed'))
        frozen = self.root/'fixture.lock.json'
        write_json(frozen, manifest)
        with patch.object(launcher, 'validate_protocol', return_value=([self.case], None)), \
                patch('main.adapters', return_value=(UntriggeredFailure(), FreshFixtureAttacker())):
            result = launcher.run_case(frozen, self.case['case_id'], run/self.case['case_id'])
        self.assertEqual(result['target_executions'], 5)
        return run

    def report(self, run):
        with patch('scripts.verify_observable_pilot5_20261029.verify_episode',
                   return_value=dict(status='native_scores_recomputed')):
            return analyze(run, verify_completed=True)

    def test_real_runner_confirmation_is_replayed_and_reported_separately(self):
        run = self.run_fixture()
        report = self.report(run)
        self.assertEqual(report['policy_id'], POLICY)
        self.assertEqual(report['design']['policy_id'], POLICY)
        self.assertEqual(report['asr']['complete_failures'], 1)
        self.assertEqual(report['target_completed'], 5)
        self.assertEqual(report['verification']['status'], 'passed')
        self.assertEqual(report['verification']['decisions_replayed'], 5)
        self.assertEqual(report['retry']['p0_confirmation_decisions'], 1)
        self.assertEqual(report['retry']['evidence_driven_decisions'], 0)
        self.assertEqual(report['retry']['unknown_fallback_decisions'], 0)
        self.assertEqual(report['retry']['other_reason_decisions'], 0)
        timeline = report['cases'][0]['timeline']
        self.assertEqual(timeline[1]['action_reason'], 'p0_confirm_trigger_false')
        self.assertEqual(timeline[0]['payload_sha256'], timeline[1]['payload_sha256'])
        self.assertNotEqual(timeline[1]['payload_sha256'], timeline[2]['payload_sha256'])
        self.assertIn(POLICY, render_markdown(report))
        self.assertIn('P0 确认 1', render_markdown(report))

    def test_beta_metrics_are_passed_through_without_controlling_replay(self):
        run = self.run_fixture()
        path = run/self.case['case_id']/'metrics.json'
        metrics = json.loads(path.read_text())
        statistical = [dict(case_id=self.case['case_id'], payload_sha256='fixture',
                            alpha=999, beta=1, mean=0.999, reporting_only=True,
                            used_for_decisions=False)]
        metrics['policies'][POLICY]['trigger_posteriors'] = statistical
        write_json(path, metrics)
        report = self.report(run)
        self.assertEqual(report['cases'][0]['trigger_posteriors'], statistical)
        self.assertEqual(report['trigger_posteriors']['by_case'][self.case['case_id']], statistical)
        self.assertTrue(report['trigger_posteriors']['statistics_only'])
        self.assertFalse(report['trigger_posteriors']['used_for_decisions'])
        self.assertEqual(report['verification']['status'], 'passed')
        self.assertEqual(report['retry']['p0_confirmation_decisions'], 1)
        self.assertIn('仅用于统计，不参与策略决策', render_markdown(report))

    def test_new_policy_decision_tampering_is_not_replayed_as_baseline(self):
        run = self.run_fixture()
        path = run/self.case['case_id']/'decisions.jsonl'
        rows = [json.loads(s) for s in path.read_text().splitlines()]
        self.assertEqual(rows[0]['reason'], 'p0_confirm_trigger_false')
        rows[0].update(action='Revise', reason='trigger_false')
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        report = self.report(run)
        self.assertEqual(report['verification']['failed_cases'], 1)
        self.assertIn('policy_decision_mismatch',
                      [x['code'] for x in report['cases'][0]['verification']['issues']])


if __name__ == '__main__':
    unittest.main()
