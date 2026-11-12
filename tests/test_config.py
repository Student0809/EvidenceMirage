from copy import deepcopy
import unittest
from src.engine.config import ROOT, load_config, validate, freeze, check_frozen, without_source_hashes
from src.evidence.ledger import validate_goal_spec
from src.evidence.observer import FixedObserver


class ConfigTests(unittest.TestCase):
    def test_empty_test_is_not_replaced_by_development_cases(self):
        config=load_config(ROOT/'experiments/e1_main.yaml')
        with self.assertRaisesRegex(ValueError,'Dataset is empty'):validate(config)

    def test_freeze_detects_dataset_mutation(self):
        config=load_config(ROOT/'experiments/e0_smoke.yaml')
        manifest=freeze(config)
        self.assertEqual(len(check_frozen(manifest)),10)
        manifest['inputs_sha256'][config['dataset']]='changed'
        with self.assertRaisesRegex(ValueError,'bytes changed'):check_frozen(manifest)

    def test_source_hashes_are_not_generated_or_checked(self):
        manifest=freeze(load_config(ROOT/'experiments/e0_smoke.yaml'))
        self.assertFalse(any(path.endswith('.py') for path in manifest['inputs_sha256']))
        self.assertNotIn('source_project', manifest)
        # Old lock files may contain changed, missing, or added source paths.
        manifest['inputs_sha256'].update({'src/schema.py': 'old-source',
                                          'src/deleted.py': 'missing-source',
                                          'main.py': 'old-entry'})
        self.assertEqual(len(check_frozen(manifest)),10)

    def test_new_run_drops_legacy_source_fields_without_changing_saved_manifest(self):
        saved = dict(inputs_sha256={'src/schema.py': 'old', 'data/dev.jsonl': 'data'},
                     source_project='old-code', launcher_sha256='old-launcher',
                     credential_helper_sha256='old-helper', verifier_sha256='old-verifier',
                     native_verifier_sha256='old-native', config={'seed': 42})
        original = deepcopy(saved)
        current = without_source_hashes(saved)
        self.assertEqual(current, dict(inputs_sha256={'data/dev.jsonl': 'data'}, config={'seed': 42}))
        self.assertEqual(saved, original)

    def test_freeze_detects_runtime_mutation(self):
        manifest=freeze(load_config(ROOT/'experiments/e0_smoke.yaml'))
        manifest['environment']['agentdojo']='different'
        with self.assertRaisesRegex(ValueError,'versions changed'):check_frozen(manifest)

    def test_invalid_fixed_queries_rejected_before_execution(self):
        with self.assertRaises(ValueError):FixedObserver(['private_deletion_state'])
        with self.assertRaises(ValueError):FixedObserver(['email_received','email_received'])

    def test_dependency_cycle_rejected(self):
        spec=dict(trigger_ids=['x'],predicates=[dict(id='x',query='email_received',kind='event',
                                                     public_goal_quote='email',depends_on=['x'])])
        with self.assertRaisesRegex(ValueError,'Cyclic'):validate_goal_spec(spec)


if __name__=='__main__':unittest.main()
