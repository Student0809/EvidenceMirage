"""Incomplete pilot reports must expose all planned slots and execution status."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.analyze_revise_ablation_20261030 import analyze, bootstrap_case_interval, markdown


class AblationAnalysisTests(unittest.TestCase):
    def test_incomplete_records_preserve_slots_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            for name, value in {
                'manifest': {'case_ids': ['c000', 'c001'], 'source_run': 'source'},
                'status': {'status': 'running'},
                'results': [{'case_id': 'c000', 'arms': {'full_original': {
                    'trials': [{'repeat': 0, 'gold': True}],
                    'generation': {'calls': []}, 'generation_status': 'ready'}}}],
            }.items():
                (run / (name + '.json')).write_text(json.dumps(value))
            report = analyze(run)
        self.assertEqual(report['case_trials']['c000']['full_original'], ['T', '?'])
        self.assertEqual(report['case_trials']['c001']['full_original'], ['?', '?'])
        self.assertEqual(report['arms']['full_original']['missing_slots'], 3)
        self.assertIn('`running`', markdown(report))
        self.assertIn('T/?', markdown(report))

    def test_bootstrap_operates_on_case_differences(self):
        self.assertIsNone(bootstrap_case_interval([]))
        self.assertEqual(bootstrap_case_interval([0, 0, 0, 0, 0]), [0, 0])
        self.assertEqual(bootstrap_case_interval([.5] * 5), [.5, .5])


if __name__ == '__main__':
    unittest.main()
