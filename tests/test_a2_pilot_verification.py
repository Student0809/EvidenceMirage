"""Independent A2 prompt and native-score verification, without model APIs."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from scripts.verify_a2_observable_pilot5_20261029 import (
    A2Reconstruction, VerificationError, read, snapshot_lines, verify_a2_case, verify_generations)
from src.adapters.agentdojo import AgentDojoAdapter
from src.attacker.a2 import A2PIMinerAdapter
from src.engine.config import ROOT, load_config, load_cases
from src.engine.runner import Runner
from src.engine.storage import RunStore, write_json
from src.evidence.observer import FixedObserver
from src.policies.registry import build_policies
from src.vendor import router


class A2PilotVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'case'
        self.config = load_config(ROOT / 'experiments/a2_observable_pilot5.yaml')
        self.case = deepcopy(next(case for case in load_cases(self.config) if case['case_id'] == 'c004'))
        self.manifest = dict(config=self.config, inputs_sha256={}, environment={},
            cases=[self.case], case_ids=[self.case['case_id']])
        self.ids = [strategy['id'] for strategy in router._discover_strategies('agentdojo')[:3]]
        self.reference = A2Reconstruction(self.config)

    def run_offline(self, fail_generation=False):
        calls = []
        def decide(prompt, stage):
            calls.append(stage)
            if stage == 'router':
                return '<choice>' + ', '.join(self.ids) + '</choice>'
            if fail_generation:
                raise TimeoutError('offline fake generator')
            return f'<analysis>analysis {len(calls)}</analysis><injection>candidate {len(calls)}</injection>'
        def query(prompt, runtime, env):
            # Invoke only the native in-process environment/checkers; no model.
            return prompt, runtime, env, [dict(role='assistant',
                content=[dict(type='text', content='No operation performed.')], tool_calls=[])], {}
        attacker = A2PIMinerAdapter(ROOT, 'offline', self.config['target']['model'],
            'strategy_library_a2/library.json', 'data/a2/dev.retrieval.json', decide=decide)
        runner = Runner(AgentDojoAdapter(pipeline=SimpleNamespace(query=query)), attacker,
            FixedObserver(**self.config['observation']), build_policies(self.config['policies']),
            RunStore(self.path, self.manifest), post_failure_budget=3, seed=self.config['seed'])
        runner.run([self.case])
        return runner

    def test_exact_prompts_native_scores_and_branch_histories(self):
        runner = self.run_offline()
        row = verify_a2_case(self.path, self.case, self.manifest, self.reference)
        self.assertEqual(row['status'], 'verified')
        self.assertEqual(row['target_executions'], 7)
        generation = row['generation_verification']
        self.assertEqual(generation['router_calls'], 1)
        self.assertEqual(generation['prompt_checks'], len(runner.generations) + 1)
        self.assertTrue(all(episode['status'] == 'native_scores_recomputed' for episode in row['episodes']))
        for item in generation['generations'][1:]:
            pid = item['generation_id'].split('/')[1]
            self.assertTrue(all(eid.split('/')[1] in ('shared', pid) for eid in item['visible_history_ids']))

    def test_added_evidence_or_foreign_history_fails_exact_prompt_check(self):
        self.run_offline()
        path = self.path / 'raw/c004/stage_aware/P1.generation.json'
        record = read(path)
        record['calls'][0]['prompt'] += '\nPRIVATE_EVIDENCE from another arm'
        write_json(path, record)
        with self.assertRaisesRegex(VerificationError, 'attacker_prompt_or_branch_history_mismatch'):
            verify_generations(self.path, self.case, self.manifest, self.reference)

    def test_tampered_case_selection_metadata_is_detected(self):
        self.run_offline()
        path = self.path / 'raw/c004/shared/P0.generation.json'
        record = read(path)
        sid = record['routing']['strategy_ids'][0]
        record['routing']['a2']['selections'][sid]['case_ids'] = []
        write_json(path, record)
        with self.assertRaisesRegex(VerificationError, 'a2_route_metadata_mismatch'):
            verify_generations(self.path, self.case, self.manifest, self.reference)

    def test_generated_payload_must_equal_executed_payload(self):
        self.run_offline()
        path = self.path / 'raw/c004/shared/000.json'
        saved = read(path)
        saved['payload'] = 'different execution'
        write_json(path, saved)
        with self.assertRaisesRegex(VerificationError, 'target_raw_payload_mismatch'):
            verify_generations(self.path, self.case, self.manifest, self.reference)

    def test_inflight_request_is_incomplete_and_strict_verification_rejects(self):
        self.run_offline()
        events, _ = snapshot_lines(self.path / 'events.jsonl')
        stop = next(i for i, event in enumerate(events) if event['event'] == 'GENERATION_STARTED'
                    and event['generation_id'] != 'c004/shared/P0')
        events = events[:stop + 1]
        (self.path / 'events.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in events))
        snapshot = verify_generations(self.path, self.case, self.manifest, self.reference, partial=True)
        self.assertEqual(snapshot['status'], 'verified_snapshot')
        self.assertEqual(snapshot['pending_requests'], [events[-1]['generation_id']])
        with self.assertRaisesRegex(VerificationError, 'unfinished_model_or_target_request'):
            verify_generations(self.path, self.case, self.manifest, self.reference)

    def test_initial_generation_error_preserves_null_outcomes_and_audits_prompts(self):
        self.run_offline(fail_generation=True)
        row = verify_a2_case(self.path, self.case, self.manifest, self.reference)
        self.assertEqual(row['status'], 'initial_generation_error_preserved')
        self.assertEqual(row['target_executions'], 0)
        self.assertEqual(row['generation_verification']['prompt_checks'], 2)
        self.assertTrue(all(outcome['gold_success'] is None for outcome in row['outcomes']))


if __name__ == '__main__':
    unittest.main()
