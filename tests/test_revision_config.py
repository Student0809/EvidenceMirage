"""Reject ambiguous revision profiles and freeze aligned retrieval dependencies."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from src.engine.config import ROOT, attacker_input_paths, load_config, validate


class RevisionConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / 'experiments/e0_native_dev.yaml')

    def test_unknown_revision_profile_rejected_before_run(self):
        self.config['attacker']['revision_profile'] = 'calibrtaed'
        with self.assertRaisesRegex(ValueError, 'revision_profile'):
            validate(self.config)

    def test_evidence_false_mode_rejects_unsupported_adapter_or_profile(self):
        self.config['attacker']['false_revision_mode'] = 'grounded'
        with self.assertRaisesRegex(ValueError, 'false_revision_mode'):
            validate(self.config)
        config = load_config(ROOT / 'experiments/evidence_piminer_aligned_asr5_20261030.yaml')
        config['attacker']['false_revision_mode'] = 'typo'
        with self.assertRaisesRegex(ValueError, 'false_revision_mode'):
            validate(config)
        config['attacker'].update(false_revision_mode='grounded', revision_profile='calibrated')
        with self.assertRaisesRegex(ValueError, 'original black-box profile'):
            validate(config)

    def test_original_cannot_silently_ignore_retrieval_options(self):
        self.config['attacker']['a2_retrieval'] = 'data/a2/dev.retrieval.json'
        with self.assertRaisesRegex(ValueError, 'A2 resources'):
            validate(self.config)

    def test_aligned_requires_both_retrieval_inputs(self):
        config = load_config(ROOT / 'experiments/evidence_piminer_aligned_asr5_20261030.yaml')
        config['attacker']['a2_library'] = 'strategy_library_a2/library.json'
        with self.assertRaisesRegex(ValueError, 'A2 resources'):
            validate(config)

    def test_aligned_freeze_includes_and_validates_retrieval(self):
        config = load_config(ROOT / 'experiments/evidence_piminer_aligned_asr5_20261030.yaml')
        config['attacker'].update(a2_library='strategy_library_a2/library.json',
                                  a2_retrieval='data/a2/dev.retrieval.json')
        cases = [{'case_id': 'c000'}]
        bundle = Mock(case_ids=('c000',), input_paths={ROOT / 'bundle.json'})
        resources = Mock(input_paths={ROOT / 'library.json', ROOT / 'retrieval.json'})
        with patch('src.engine.config.initialization_bundle', return_value=bundle), \
                patch('src.attacker.a2.A2Resources', return_value=resources):
            paths = attacker_input_paths(config, cases)
        resources.validate_case.assert_called_once_with(cases[0])
        self.assertTrue(resources.input_paths <= paths)
        self.assertTrue(bundle.input_paths <= paths)


if __name__ == '__main__':
    unittest.main()
