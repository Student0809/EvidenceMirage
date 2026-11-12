"""Native offline fixtures for matched baseline integrity verification."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from scripts.run_piminer_asr3_20261029 import ROOT, ReusedInitialRunner, load_initial
from scripts.verify_piminer_asr3_20261029 import expected_prompt, verify_case, VerificationError
from src.adapters.agentdojo import AgentDojoAdapter
from src.attacker.original import OriginalPIMinerAdapter
from src.engine.storage import RunStore, write_json
from src.evidence.observer import FixedObserver
from src.policies.always_revise import AlwaysRevise


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.source = ROOT / 'runs/observable_pilot5_20261029'
        if not self.source.exists():
            self.skipTest('Local source artifacts unavailable')
        original = json.loads((self.source / 'manifest.json').read_text())
        self.case = next(c for c in original['cases'] if c['case_id'] == 'c004')
        self.initial = load_initial(self.source, self.case)
        config = deepcopy(original['config'])
        config.update(post_failure_target_budget=2, policies={'always_revise': {}})
        self.manifest = dict(config=config, cases=[self.case], case_ids=['c004'],
            source_run=str(self.source), reused_initial=True, initial_provenance=self.initial['provenance'],
            inputs_sha256={}, environment={})
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'c004'

    def create_run(self):
        def query(prompt, runtime, env):
            return prompt, runtime, env, [dict(role='assistant',
                content=[dict(type='text', content='Offline fixture completed.')], tool_calls=[])], {}
        def decide(prompt, stage):
            return '<analysis>Offline test.</analysis><injection>Fixture text.</injection>'
        config = self.manifest['config']
        target = AgentDojoAdapter(pipeline=SimpleNamespace(query=query))
        attacker = OriginalPIMinerAdapter(ROOT, model='offline', target_model=config['target']['model'], decide=decide)
        runner = ReusedInitialRunner(target, attacker, FixedObserver(**config['observation']),
            {'always_revise': AlwaysRevise()}, RunStore(self.path, self.manifest),
            initial=self.initial, post_failure_budget=2)
        runner.run([self.case])

    def test_native_scores_prompts_and_two_new_executions(self):
        self.create_run()
        report = verify_case(self.path, self.case, self.manifest)
        self.assertEqual(report['new_target_executions'], 2)
        self.assertEqual(report['reused_initial_executions'], 1)
        self.assertEqual(report['revision_prompts_verified'], 2)
        self.assertTrue(all(e['status'] == 'native_scores_recomputed' for e in report['episodes']))

    def test_prompt_history_tampering_is_detected(self):
        self.create_run()
        path = self.path / 'raw/c004/always_revise/P1.generation.json'
        record = json.loads(path.read_text())
        record['calls'][0]['prompt'] += '\nUndeclared history'
        write_json(path, record)
        with self.assertRaisesRegex(VerificationError, 'revision_prompt_history_mismatch'):
            verify_case(self.path, self.case, self.manifest)

    def test_reused_initial_cannot_be_charged_as_new(self):
        self.create_run()
        path = self.path / 'metrics.json'
        record = json.loads(path.read_text())
        record['physical_total_cost']['target_executions'] = 3
        write_json(path, record)
        with self.assertRaisesRegex(VerificationError, 'new_cost_mismatch'):
            verify_case(self.path, self.case, self.manifest)

    def test_all_five_restored_first_revision_prompts_match(self):
        original = json.loads((self.source / 'manifest.json').read_text())
        for case in original['cases']:
            initial = load_initial(self.source, case)
            path = self.source / case['case_id'] / 'raw' / case['case_id'] / 'stage_aware/P1.generation.json'
            recorded = json.loads(path.read_text())['calls'][0]['prompt']
            self.assertEqual(expected_prompt(case, initial['route']['document'], [initial['attempt']]), recorded)


if __name__ == '__main__':
    unittest.main()
