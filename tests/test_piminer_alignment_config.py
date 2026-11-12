"""Offline aligned-mode config, input freeze, entry point and launch contract."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from scripts import run_evidence_parallel as launcher
from src.attacker.initialization import InitializationBundle
from src.engine.config import (ROOT, attacker_input_paths, digest, fingerprint,
                               load_config, validate)


class AlignmentConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / 'experiments/evidence_piminer_aligned_asr5_20261030.yaml')

    def test_same_selected22_target_and_controller_with_explicit_aligned_attacker(self):
        previous = load_config(ROOT / 'experiments/evidence_piminer_test_asr5_20261030.yaml')
        for key in ('target', 'seed', 'dataset', 'goal_specs', 'benchmark_audit', 'observation',
                    'policies', 'post_failure_target_budget', 'evaluation_scope'):
            self.assertEqual(self.config[key], previous[key], key)
        self.assertEqual(self.config['attacker'], dict(mode='piminer_aligned',
            model='deepseek-flash[1m]', effort='max', timeout=900,
            initialization_manifest='data/piminer_aligned_20261030/initializations.json'))
        cases = validate(self.config)
        self.assertEqual(len(cases), 22)
        self.assertEqual(self.config['post_failure_target_budget'] + 1, 5)
        self.assertEqual(self.config['experiment']['case_parallelism'], 16)
        self.assertFalse(self.config['experiment']['reused_initial'])
        self.assertTrue(self.config['experiment']['reuse_previous_routes'])
        self.assertTrue(self.config['experiment']['reuse_previous_payloads'])
        self.assertEqual(previous['attacker']['mode'], 'original')
        self.assertFalse(previous['experiment']['reuse_previous_payloads'])

    def test_all_initialization_and_strategy_files_enter_freeze(self):
        cases = validate(self.config)
        bundle = InitializationBundle(ROOT, self.config['attacker']['initialization_manifest'])
        paths = attacker_input_paths(self.config, cases)
        self.assertTrue(bundle.input_paths <= paths)
        self.assertIn(ROOT / 'src/vendor/attacker_role.txt', paths)
        hashes = fingerprint(self.config, cases)
        for path in bundle.input_paths:
            self.assertEqual(hashes[str(path.relative_to(ROOT))], digest(path))
        self.assertIn('data/piminer_aligned_20261030/strategies/_template_cold_start.md', hashes)
        self.assertFalse(any(key.startswith('../') for key in hashes))

    def test_missing_manifest_wrong_backend_external_path_and_partial_cases_rejected(self):
        for mutation in ('missing', 'fixture', 'outside'):
            config = deepcopy(self.config)
            if mutation == 'missing':
                config['attacker'].pop('initialization_manifest')
            elif mutation == 'fixture':
                config.update(backend='fixture', split='dev')
            else:
                config['attacker']['initialization_manifest'] = '../outside.json'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate(config)
        cases = validate(self.config)
        for bad in (cases[:-1], list(reversed(cases))):
            with self.assertRaisesRegex(ValueError, 'full dataset in order'):
                attacker_input_paths(self.config, bad)
        cases[0]['context_with_placeholder'] += ' identity drift'
        with self.assertRaisesRegex(ValueError, 'identity drift'):
            attacker_input_paths(self.config, cases)

    def test_main_instantiates_aligned_with_complete_options_without_api(self):
        from main import adapters
        aligned = Mock()
        module = types.ModuleType('src.attacker.aligned')
        module.PIMinerAlignedAdapter = aligned
        with patch.dict('sys.modules', {'src.attacker.aligned': module}), \
                patch('src.adapters.agentdojo.AgentDojoAdapter') as target:
            result = adapters(self.config)
        aligned.assert_called_once_with(ROOT, target_model='gpt-5-nano',
            model='deepseek-flash[1m]', effort='max', timeout=900,
            initialization_manifest='data/piminer_aligned_20261030/initializations.json')
        target.assert_called_once_with(**self.config['target'])
        self.assertEqual(result, (target.return_value, aligned.return_value))

    def test_launcher_limits_distinguish_import_from_generation_and_no_old_h1(self):
        limits = launcher.execution_limits(self.config, 22)
        self.assertEqual(limits, dict(total_target_budget_per_case=5,
            max_target_executions=110, max_router_calls=0,
            max_payload_generation_calls=88, preflight_model_calls=3))
        metadata = launcher.initialization_metadata(self.config)
        self.assertTrue(metadata['fresh_initial'])
        self.assertFalse(metadata['reused_initial'])
        self.assertTrue(metadata['reused_route'])
        self.assertTrue(metadata['reused_payload'])
        original = deepcopy(self.config)
        original['attacker']['mode'] = 'original'
        self.assertEqual(launcher.execution_limits(original, 22)['max_router_calls'], 22)
        self.assertEqual(launcher.execution_limits(original, 22)['max_payload_generation_calls'], 110)
        self.assertFalse(launcher.initialization_metadata(original)['reused_payload'])

    def test_protocol_accepts_aligned_and_keeps_reference_runtime_equality(self):
        cases = validate(self.config)
        manifest = dict(config=self.config, inputs_sha256={})
        with patch.object(launcher, 'check_frozen', return_value=cases):
            self.assertEqual(launcher.validate_protocol(manifest), (cases, None))
            with tempfile.TemporaryDirectory() as temporary:
                reference = Path(temporary)
                ref = dict(manifest, cases=cases)
                (reference / 'manifest.json').write_text(json.dumps(ref))
                _, comparison = launcher.validate_protocol(manifest, reference)
                self.assertIn('Target H1 is freshly executed', comparison['comparison_rule'])
                ref['config'] = deepcopy(self.config)
                ref['config']['attacker']['mode'] = 'original'
                (reference / 'manifest.json').write_text(json.dumps(ref))
                with self.assertRaisesRegex(ValueError, 'Reference runtime configuration differs'):
                    launcher.validate_protocol(manifest, reference)

    def test_dry_run_uses_configured_16_workers_and_never_connects_or_creates_run(self):
        cases = validate(self.config)
        with tempfile.TemporaryDirectory() as temporary:
            frozen = Path(temporary) / 'input.json'
            frozen.write_text(json.dumps(dict(config=self.config)))
            output = Path(temporary) / 'not-created'
            stdout = io.StringIO()
            argv = ['run_evidence_parallel.py', '--frozen', str(frozen), '--output', str(output), '--dry-run']
            with patch('sys.argv', argv), patch.object(launcher, 'check_frozen', return_value=cases), \
                    patch('src.adapters.agentdojo.validate_native_cases', return_value={'status': 'offline_valid'}), \
                    patch.object(launcher, 'configure_credentials', side_effect=AssertionError('No API')), \
                    patch.object(launcher, 'launch_workers', side_effect=AssertionError('No launch')), \
                    redirect_stdout(stdout):
                launcher.main()
            result = json.loads(stdout.getvalue())
            self.assertEqual(result['workers'], 16)
            self.assertEqual(result['api_calls'], 0)
            self.assertTrue(result['reused_route'])
            self.assertTrue(result['reused_payload'])
            self.assertFalse(result['reused_initial'])
            self.assertEqual(result['limits']['max_router_calls'], 0)
            self.assertFalse(output.exists())

    def test_worker_manifest_and_metrics_record_import_without_reusing_initial_outcome(self):
        cases = validate(self.config)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frozen = root / 'input.json'
            frozen.write_text(json.dumps(dict(config=self.config)))
            output = root / cases[0]['case_id']
            runner = Mock(outcomes=[])
            runner.run.return_value = dict(physical_total_cost={'target_executions': 1})
            with patch.object(launcher, 'validate_protocol', return_value=(cases, None)), \
                    patch('main.adapters', return_value=(object(), object())), \
                    patch('src.engine.runner.Runner', return_value=runner):
                result = launcher.run_case(frozen, cases[0]['case_id'], output)
            self.assertEqual(result['target_executions'], 1)
            for filename in ('manifest.json', 'metrics.json'):
                saved = json.loads((output / filename).read_text())
                self.assertTrue(saved['fresh_initial'])
                self.assertTrue(saved['reused_route'])
                self.assertTrue(saved['reused_payload'])
                self.assertFalse(saved['reused_initial'])
                self.assertEqual(saved['total_target_budget'], 5)
            self.assertEqual(json.loads((output / 'manifest.json').read_text())['parent_run'], str(root))


if __name__ == '__main__':
    unittest.main()
