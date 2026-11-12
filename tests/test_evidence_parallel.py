"""Offline protocol and process-isolation checks for the parallel launcher."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_evidence_parallel as launcher
from src.engine.config import load_config, freeze
from src.adapters.fixture import FixtureAttacker
from src.evidence.observer import policy_for_case
from src.schema import TargetResult
from src.vendor.attacker_observer import Collector


_WORKER_HISTORY = []


def isolated_job(frozen, cid, output):
    if cid == 'bad':
        raise RuntimeError('Synthetic worker failure')
    _WORKER_HISTORY.append(cid)
    return dict(case_id=cid, status='completed', pid=os.getpid(), history=list(_WORKER_HISTORY))


class FreshFixtureAttacker(FixtureAttacker):
    use_cached_p0 = False


class AlwaysTriggeredFailure:
    def execute(self, case, payload, episode_id):
        collector = Collector(policy_for_case(case))
        collector.mail(collector.policy['mailboxes'], 'Fixture', 'Synthetic receipt', [])
        return TargetResult(False, observation=collector.finish('Fixture only.'))


class ParallelEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(launcher.ROOT / 'experiments/evidence_repeat_asr3_pilot5_20261030.yaml')

    def test_reference_protocol_has_same_five_cases_and_two_remaining_executions(self):
        cases, comparison = launcher.validate_protocol(freeze(self.config), launcher.ROOT / 'runs/observable_pilot5_20261029')
        self.assertEqual([c['case_id'] for c in cases], ['c000','c001','c004','c005','c006'])
        self.assertEqual(self.config['post_failure_target_budget'], 2)
        self.assertTrue(comparison['case_snapshots_equal'])
        self.assertTrue(comparison['controller_settings_equal'])
        self.assertEqual(comparison['reference_post_failure_budget'], 3)

    def test_reference_comparison_ignores_only_python_fingerprints(self):
        cases = [dict(case_id='fixture')]
        old = dict(config=self.config, cases=cases,
                   inputs_sha256={'main.py': 'old-code', 'data/cases.jsonl': 'same-data'})
        current = dict(config=self.config,
                       inputs_sha256={'main.py': 'new-code', 'data/cases.jsonl': 'same-data',
                                      'src/new.py': 'new-code'})
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory)
            (reference / 'manifest.json').write_text(json.dumps(old))
            with patch.object(launcher, 'check_frozen', return_value=cases):
                _, comparison = launcher.validate_protocol(current, reference)
                self.assertEqual(comparison['changed_previous_input_paths'], [])
                current['inputs_sha256']['data/cases.jsonl'] = 'changed-data'
                _, comparison = launcher.validate_protocol(current, reference)
                self.assertEqual(comparison['changed_previous_input_paths'], ['data/cases.jsonl'])

    def test_different_model_and_controller_cannot_be_called_repetition(self):
        for key in ('target', 'policies'):
            config = load_config(launcher.ROOT / 'experiments/evidence_repeat_asr3_pilot5_20261030.yaml')
            if key == 'target':
                config[key]['model'] = 'changed-model'
            else:
                config[key]['stage_aware']['max_retries_per_payload'] = 1
            with self.assertRaisesRegex(ValueError, 'Reference'):
                launcher.validate_protocol(freeze(config), launcher.ROOT / 'runs/observable_pilot5_20261029')

    def test_worker_failure_does_not_drop_other_cases_or_repeat_job(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(launcher, 'run_case', isolated_job):
                results = launcher.launch_workers('unused', [{'case_id':'ok'}, {'case_id':'bad'}], Path(temp), 2)
            saved = json.loads((Path(temp) / 'worker_results.json').read_text())
        by_id = {r['case_id']: r for r in results}
        self.assertEqual(len(results), 2)
        self.assertEqual(by_id['bad']['status'], 'worker_error')
        self.assertEqual(by_id['ok']['status'], 'completed')
        self.assertNotEqual(by_id['ok']['pid'], os.getpid())
        self.assertEqual(by_id['ok']['history'], ['ok'])
        self.assertEqual(_WORKER_HISTORY, [])
        self.assertEqual([r['case_id'] for r in saved], ['bad','ok'])

    def test_launcher_accepts_benchmark_but_not_strict_heldout_test(self):
        manifest = dict(config=dict(self.config, split='benchmark'))
        # Audit validation belongs to check_frozen; verify the launcher's split boundary.
        with patch.object(launcher, 'check_frozen', return_value=[dict(case_id='c000')]):
            cases, comparison = launcher.validate_protocol(manifest)
            self.assertEqual(cases[0]['case_id'], 'c000')
            self.assertIsNone(comparison)
            manifest['config']['split'] = 'test'
            with self.assertRaisesRegex(ValueError, 'audited benchmark'):
                launcher.validate_protocol(manifest)

    def test_full_population_benchmark_requires_explicit_unobservable_opt_in(self):
        config = dict(self.config, split='benchmark', require_observable_trigger=False,
                      allow_unobservable_trigger=True)
        with patch.object(launcher, 'check_frozen', return_value=[dict(case_id='c000')]):
            cases, _ = launcher.validate_protocol(dict(config=config))
            self.assertEqual(cases[0]['case_id'], 'c000')
            del config['allow_unobservable_trigger']
            with self.assertRaisesRegex(ValueError, 'explicit audited opt-in'):
                launcher.validate_protocol(dict(config=config))
            config['allow_unobservable_trigger'] = True
            config['split'] = 'dev'
            with self.assertRaisesRegex(ValueError, 'explicit audited opt-in'):
                launcher.validate_protocol(dict(config=config))

    def test_total_five_budget_preserves_retry_cap_and_fresh_generation(self):
        self.config['post_failure_target_budget'] = 4
        manifest = freeze(self.config)
        manifest['inputs_sha256']['main.py'] = 'obsolete-python-digest'
        manifest.update(launcher_sha256='obsolete-python-digest',
                        credential_helper_sha256='obsolete-python-digest',
                        verifier_sha256='obsolete-python-digest',
                        native_verifier_sha256='obsolete-python-digest', source_project={'commit': 'old-source'})
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            frozen = directory / 'frozen.json'
            frozen.write_text(json.dumps(manifest))
            output = directory / 'c000'
            with patch('main.adapters', return_value=(AlwaysTriggeredFailure(), FreshFixtureAttacker())):
                result = launcher.run_case(frozen, 'c000', output)
            episodes = [json.loads(line) for line in (output / 'episodes.jsonl').read_text().splitlines()]
            decisions = [json.loads(line) for line in (output / 'decisions.jsonl').read_text().splitlines()]
            child = json.loads((output / 'manifest.json').read_text())
            metrics = json.loads((output / 'metrics.json').read_text())
            events = [json.loads(line) for line in (output / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(result['target_executions'], 5)
        self.assertEqual([d['action'] for d in decisions], ['Retry', 'Retry', 'Revise', 'Retry', 'Stop'])
        self.assertEqual(decisions[-1]['reason'], 'target_budget_exhausted')
        self.assertEqual(decisions[2]['reason'], 'payload_retry_cap')
        self.assertEqual(episodes[0]['payload_sha256'], episodes[2]['payload_sha256'])
        self.assertNotEqual(episodes[2]['payload_sha256'], episodes[3]['payload_sha256'])
        self.assertEqual(episodes[3]['payload_sha256'], episodes[4]['payload_sha256'])
        self.assertEqual(sum(e['event'] == 'GENERATION_STARTED' for e in events), 2)
        self.assertTrue(any(e.get('generation_id') == 'c000/shared/P0' for e in events))
        self.assertNotIn('main.py', child['inputs_sha256'])
        self.assertFalse({'launcher_sha256', 'credential_helper_sha256', 'verifier_sha256',
                          'native_verifier_sha256', 'source_project'} & set(child))
        self.assertEqual(child['total_target_budget'], 5)
        self.assertFalse(child['independent_heldout'])
        self.assertEqual(child['evaluation_scope'], 'development')
        self.assertEqual(metrics['total_target_budget'], 5)

    def test_evidence_false_budget_counts_planning_and_bounded_repair(self):
        config = dict(self.config, post_failure_target_budget=4,
            attacker=dict(self.config['attacker'], mode='piminer_aligned',
                          false_revision_mode='grounded'))
        limits = launcher.execution_limits(config, 30)
        self.assertEqual(limits['max_target_executions'], 150)
        self.assertEqual(limits['max_router_calls'], 0)
        self.assertEqual(limits['max_revision_requests'], 120)
        self.assertEqual(limits['grounded_revision_protocol'], 'grounded_history_plan_v2')
        self.assertEqual(limits['max_planner_calls'], 480)
        self.assertEqual(limits['max_semantic_review_calls'], 240)
        self.assertEqual(limits['max_payload_generation_calls'], 240)
        self.assertEqual(limits['max_attacker_model_calls'], 960)
        self.assertEqual(limits['preflight_model_calls'], 4)

    def test_terminal_source_metadata_does_not_call_it_original_p0(self):
        config = dict(self.config, attacker=dict(self.config['attacker'], mode='piminer_aligned'),
            experiment=dict(initial_payload_source='piminer_last_evaluated_attempt'))
        metadata = launcher.initialization_metadata(config)
        self.assertEqual(metadata['initial_payload_source'], 'piminer_last_evaluated_attempt')
        self.assertEqual(metadata['initial_analysis_policy'], 'empty_exclude_historical_revision_reasoning')
        self.assertIn('last evaluated original PIMiner payload', metadata['initial_execution_note'])
        self.assertTrue(metadata['fresh_initial'])
        self.assertFalse(metadata['reused_initial'])

    def test_legacy_initialization_metadata_remains_unchanged(self):
        config = dict(self.config, attacker=dict(self.config['attacker'], mode='piminer_aligned'))
        self.assertEqual(launcher.initialization_metadata(config), dict(
            fresh_initial=True, reused_initial=False, reused_route=True, reused_payload=True,
            initial_execution_note='P0 is executed in a fresh Target environment; no historical H1 is reused.'))


if __name__ == '__main__':
    unittest.main()
