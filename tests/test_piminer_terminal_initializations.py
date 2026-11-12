"""Offline provenance checks for the original last-executed-payload import."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.prepare_piminer_alignment import prepare as prepare_first
from scripts.prepare_piminer_terminal_initializations_20261031 import prepare
from src.attacker.initialization import InitializationBundle, sha256_bytes


class TerminalInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.source = Path(self.temp.name) / 'source'
        self.root.mkdir()
        (self.source / 'samples').mkdir(parents=True)
        (self.source / 'attempts').mkdir()
        (self.source / 'strategies').mkdir()
        strategy = self.source / 'strategies' / 'alpha.md'
        strategy.write_text('# Alpha\nExact strategy snapshot.\n')
        (self.source / 'config.json').write_text(json.dumps(dict(
            dataset='agentdojo', max_iters=5, threat_model='black_box')))
        self.cases = [dict(case_id=f'c{i:03}', suite_name='workspace',
            user_task_id=f'user_task_{i}', injection_task_id='injection_task_0',
            user_task_prompt=f'public task {i}', injection_task_goal='public goal',
            context_with_placeholder='public context <INJECTION>',
            injection_candidates=['public tool message'], step_index=i) for i in range(30)]
        for i, case in enumerate(self.cases):
            history = []
            for iteration in range(5):
                analysis = f'PRIVATE_ANALYSIS_{i}_{iteration}'
                injection = f'Exact payload {i} revision {iteration}\n'
                raw = f'<analysis>{analysis}</analysis>\n<injection>{injection}</injection>\n'
                (self.source / 'attempts' / f'{i:03}_iter{iteration}.txt').write_text(raw)
                history.append(dict(iter=iteration, analysis=analysis, injection=injection,
                    raw_attempt=raw, utility=False, security=False,
                    final_output='PRIVATE_SOURCE_FINAL', trajectory='PRIVATE_SOURCE_TRACE',
                    model_output_text='PRIVATE_MODEL_OUTPUT', target_seconds=1.25, error=None))
            sample = dict(deepcopy(case), sample_index=i, status='miss', history=history,
                          strategy_ids=['alpha'], strategy_paths=[str(strategy)])
            (self.source / 'samples' / f'{i:03}.json').write_text(json.dumps(sample))
        (self.root / 'cases.jsonl').write_text(
            ''.join(json.dumps(case) + '\n' for case in self.cases))
        self.base = prepare_first('base', root=self.root, cases_path='cases.jsonl',
                                  source_run=self.source)

    def build(self):
        return prepare('terminal', root=self.root, base_manifest='base/initializations.json',
                       report_prefix='reports/source')

    def mutate_sample(self, mutate, *, acknowledge_hash=False):
        path = self.source / 'samples/000.json'
        sample = json.loads(path.read_text())
        mutate(sample)
        path.write_text(json.dumps(sample))
        if acknowledge_hash:
            digest = sha256_bytes(path.read_bytes())
            base = json.loads(self.base.read_text())
            base['initializations'][0]['source_metadata']['sample_sha256'] = digest
            self.base.write_text(json.dumps(base))
            reference_path = self.base.parent / 'reference.json'
            reference = json.loads(reference_path.read_text())
            reference['sources'][0]['sample_sha256'] = digest
            reference_path.write_text(json.dumps(reference))

    def test_uses_actual_last_attempt_for_all_cases_and_discards_all_source_analysis(self):
        before = {path: sha256_bytes(path.read_bytes()) for path in self.source.rglob('*')
                  if path.is_file()}
        path, audit = self.build()
        bundle = InitializationBundle(self.root, path)
        base = json.loads(self.base.read_text())
        current = json.loads(path.read_text())
        self.assertEqual(len(bundle.case_ids), 30)
        for i, case in enumerate(self.cases):
            imported = bundle.for_case(case)
            self.assertEqual(imported['payload'], f'Exact payload {i} revision 4\n')
            self.assertEqual(imported['analysis'], '')
            self.assertEqual(current['initializations'][i]['analysis_sha256'],
                             sha256_bytes(b''))
            changed = {key for key in current['initializations'][i]
                       if current['initializations'][i][key] != base['initializations'][i][key]}
            self.assertEqual(changed, {'payload', 'payload_sha256', 'analysis', 'analysis_sha256'})
        for artifact in (path, path.parent / 'reference.json', self.root / 'reports/source.json'):
            text = artifact.read_text()
            self.assertNotIn('PRIVATE_ANALYSIS', text)
            self.assertNotIn('PRIVATE_SOURCE_FINAL', text)
            self.assertNotIn('PRIVATE_SOURCE_TRACE', text)
            self.assertNotIn('PRIVATE_MODEL_OUTPUT', text)
        self.assertEqual(audit['source_status_counts'], {'miss': 30})
        self.assertEqual(audit['source_terminal_iter_counts'], {'4': 30})
        self.assertFalse(audit['outcome_filter'])
        self.assertEqual(audit['api_calls'], 0)
        self.assertEqual(before, {path: sha256_bytes(path.read_bytes()) for path in before})

    def test_rejects_source_sample_hash_drift_before_writing(self):
        self.mutate_sample(lambda s: s.update(status='changed'))
        with self.assertRaisesRegex(ValueError, 'provenance changed'):
            self.build()
        self.assertFalse((self.root / 'terminal').exists())

    def test_rejects_identity_drift_even_when_source_hash_is_acknowledged(self):
        self.mutate_sample(lambda s: s.update(user_task_id='wrong_task'), acknowledge_hash=True)
        with self.assertRaisesRegex(ValueError, 'public identity differs'):
            self.build()
        self.assertFalse((self.root / 'terminal').exists())

    def test_rejects_unexecuted_generated_candidate_instead_of_importing_it(self):
        (self.source / 'attempts/000_iter5.txt').write_text(
            '<analysis>unexecuted</analysis><injection>unexecuted best candidate</injection>')
        with self.assertRaisesRegex(ValueError, 'unexecuted candidate'):
            self.build()
        self.assertFalse((self.root / 'terminal').exists())

    def test_retains_both_source_success_and_failure_without_outcome_filter(self):
        sample_path = self.source / 'samples/000.json'
        sample = json.loads(sample_path.read_text())
        sample['history'] = sample['history'][:2]
        sample['history'][-1]['security'] = True
        sample['status'] = 'hit'
        for iteration in range(2, 5):
            (self.source / 'attempts' / f'000_iter{iteration}.txt').unlink()
        self.mutate_sample(lambda s: s.update(sample), acknowledge_hash=True)
        path, audit = self.build()
        self.assertEqual(audit['source_status_counts'], {'hit': 1, 'miss': 29})
        self.assertEqual(len(audit['sources']), 30)
        self.assertEqual(InitializationBundle(self.root, path).for_case(self.cases[0])['payload'],
                         'Exact payload 0 revision 1\n')
        # Historical verdicts stay in the sidecar and never reach runtime fields.
        entry = json.loads(path.read_text())['initializations'][0]
        self.assertFalse({'security', 'source_gold', 'status', 'iter', 'history'} & set(entry))
        self.assertEqual(entry['analysis'], '')

    def test_refuses_overwrite(self):
        path, _ = self.build()
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
