"""Historical initialization isolation and identity checks; no model calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.prepare_piminer_alignment import prepare
from src.attacker.initialization import InitializationBundle, sha256_bytes


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.source = base / 'project', base / 'original'
        self.root.mkdir()
        (self.source / 'samples').mkdir(parents=True)
        (self.source / 'strategies').mkdir()
        self.cases = [dict(case_id=f'c{i:03}', suite_name='workspace',
            user_task_id=f'user_task_{i}', injection_task_id='injection_task_0',
            user_task_prompt=f'public task {i}', injection_task_goal='public goal',
            context_with_placeholder='public context <INJECTION>',
            injection_candidates=['public tool message'], step_index=i) for i in (0, 16)]
        self.strategies = {'alpha': b'# Alpha\r\nOriginal exact bytes.\r\n',
                           '_template_cold_start': b'# Cold start\nOriginal template.\n'}
        for sid, content in self.strategies.items():
            (self.source / 'strategies' / (sid + '.md')).write_bytes(content)
        for case in self.cases:
            index = int(case['case_id'][1:])
            ids = ['alpha'] if index == 0 else ['alpha', '_template_cold_start']
            sample = dict(deepcopy(case), sample_index=index, strategy_ids=ids,
                strategy_paths=[str(self.source / 'strategies' / (sid + '.md')) for sid in ids],
                status='HISTORICAL_STATUS_SENTINEL', history=[
                    dict(iter=0, analysis='Original P0 analysis', injection=f'Exact P0 {index}\n',
                        security='PRIVATE_SECURITY_SENTINEL', utility='PRIVATE_UTILITY_SENTINEL',
                        final_output='PRIVATE_FINAL_SENTINEL', trajectory='PRIVATE_TRACE_SENTINEL'),
                    dict(iter=1, analysis='FUTURE_ANALYSIS_SENTINEL',
                        injection='FUTURE_PAYLOAD_SENTINEL', security=True)])
            (self.source / 'samples' / f'{index:03}.json').write_text(json.dumps(sample))
        self.cases_path = self.root / 'cases.jsonl'
        self.cases_path.write_text(''.join(json.dumps(c) + '\n' for c in self.cases))
        self.output = self.root / 'aligned'

    def build(self):
        return prepare(self.output, root=self.root, cases_path=self.cases_path,
                       source_run=self.source)

    def mutate_bundle(self, callback):
        path = self.output / 'initializations.json'
        data = json.loads(path.read_text())
        callback(data)
        path.write_text(json.dumps(data))

    def test_exact_public_match_route_p0_analysis_and_strategy_bytes(self):
        path = self.build()
        bundle = InitializationBundle(self.root, path)
        self.assertEqual(bundle.case_ids, ('c000', 'c016'))
        result = bundle.for_case(self.cases[1])
        self.assertEqual(set(result), {'strategy_ids', 'strategy_paths', 'payload',
                                      'analysis', 'source_metadata'})
        self.assertEqual(result['payload'], 'Exact P0 16\n')
        self.assertEqual(result['analysis'], 'Original P0 analysis')
        self.assertEqual(result['strategy_ids'], ['alpha', '_template_cold_start'])
        self.assertEqual(result['strategy_paths'], ['aligned/strategies/alpha.md',
                                                  'aligned/strategies/_template_cold_start.md'])
        for sid, content in self.strategies.items():
            self.assertEqual((self.output / 'strategies' / (sid + '.md')).read_bytes(), content)
        expected = '\n\n'.join(
            f'===== Candidate strategy {i}/2: {sid} =====\n\n'
            + (self.source / 'strategies' / (sid + '.md')).read_text()
            for i, sid in enumerate(result['strategy_ids'], 1))
        self.assertEqual(bundle.strategy_document(self.cases[1]), expected)
        self.assertEqual(bundle.input_paths, {path, self.cases_path,
            self.output / 'strategies/alpha.md', self.output / 'strategies/_template_cold_start.md'})
        result['strategy_ids'].clear()
        self.assertEqual(len(bundle.for_case(self.cases[1])['strategy_ids']), 2)

    def test_no_historical_target_feedback_or_later_candidates_imported(self):
        self.build()
        for path in [self.output / 'initializations.json', self.output / 'reference.json']:
            text = path.read_text()
            self.assertNotIn('SENTINEL', text)
        data = json.loads((self.output / 'initializations.json').read_text())
        for record in data['initializations']:
            self.assertFalse({'security', 'utility', 'final_output', 'history', 'status'} & set(record))
        # Entire histories are hashed for provenance, never loaded by the runtime.
        for path in (self.source / 'samples').glob('*'):
            path.unlink()
        bundle = InitializationBundle(self.root, self.output / 'initializations.json')
        self.assertEqual(bundle.for_case(self.cases[0])['payload'], 'Exact P0 0\n')

    def test_runtime_rejects_every_public_identity_drift_and_wrong_case(self):
        bundle = InitializationBundle(self.root, self.build())
        for key in ('suite_name', 'user_task_id', 'injection_task_id', 'user_task_prompt',
                    'injection_task_goal', 'context_with_placeholder', 'injection_candidates', 'step_index'):
            case = deepcopy(self.cases[0])
            case[key] = ['changed vector'] if key == 'injection_candidates' else 9 if key == 'step_index' else 'changed'
            with self.subTest(field=key), self.assertRaises(ValueError):
                bundle.for_case(case)
        for cid in ('c016', 'c999'):
            with self.subTest(case_id=cid), self.assertRaises(ValueError):
                bundle.for_case(dict(self.cases[0], case_id=cid))
        # Added current-run annotations are not part of the public identity.
        self.assertEqual(bundle.for_case(dict(self.cases[0], goal_spec={'new': True}))['payload'], 'Exact P0 0\n')

    def test_import_rejects_wrong_source_identity(self):
        path = self.source / 'samples/000.json'
        data = json.loads(path.read_text())
        data['context_with_placeholder'] += ' drift'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'Source public identity'):
            self.build()
        self.assertFalse(self.output.exists())

    def test_rejects_duplicate_missing_and_out_of_order_bundle_entries(self):
        path = self.build()
        original = path.read_text()
        mutations = [lambda d: d['initializations'].append(deepcopy(d['initializations'][0])),
                     lambda d: d['initializations'].pop(),
                     lambda d: d['initializations'].reverse(),
                     lambda d: d['initializations'].__setitem__(1, deepcopy(d['initializations'][0])),
                     lambda d: d['case_ids'].reverse()]
        for mutation in mutations:
            path.write_text(original)
            self.mutate_bundle(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                InitializationBundle(self.root, path)

    def test_rejects_unexpected_feedback_fields_and_duplicate_json_keys(self):
        path = self.build()
        original = path.read_text()
        mutations = [lambda d: d.update(history=[]),
                     lambda d: d['initializations'][0].update(security=True),
                     lambda d: d['initializations'][0]['source_metadata'].update(final_output='private')]
        for mutation in mutations:
            path.write_text(original)
            self.mutate_bundle(mutation)
            with self.assertRaises(ValueError):
                InitializationBundle(self.root, path)
        path.write_text('{"schema_version": 1, "schema_version": 1}')
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON'):
            InitializationBundle(self.root, path)

    def test_rejects_changed_strategy_payload_analysis_and_route_order(self):
        path = self.build()
        original = path.read_text()
        mutations = [lambda d: d['initializations'][0].update(payload='drift'),
                     lambda d: d['initializations'][0].update(analysis='drift'),
                     lambda d: [d['initializations'][1][key].reverse() for key in
                                ('strategy_ids', 'strategy_paths', 'strategy_sha256')]]
        for mutation in mutations:
            path.write_text(original)
            self.mutate_bundle(mutation)
            with self.assertRaises(ValueError):
                InitializationBundle(self.root, path)
        path.write_text(original)
        (self.output / 'strategies/alpha.md').write_text('changed bytes')
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            InitializationBundle(self.root, path)

    def test_loaded_bundle_detects_input_drift_before_reuse(self):
        path = self.build()
        for changed_path in (path, self.cases_path, self.output / 'strategies/alpha.md'):
            original = changed_path.read_bytes()
            bundle = InitializationBundle(self.root, path)
            changed_path.write_bytes(original + b'\n')
            with self.subTest(path=changed_path), self.assertRaisesRegex(ValueError, 'changed after loading'):
                bundle.for_case(self.cases[0])
            changed_path.write_bytes(original)

    def test_rejects_external_paths_and_symlink_escape(self):
        with self.assertRaisesRegex(ValueError, 'inside this project'):
            prepare(self.root.parent / 'outside', root=self.root, cases_path=self.cases_path,
                    source_run=self.source)
        path = self.build()
        for bad in ('../original/strategies/alpha.md', str(self.source / 'strategies/alpha.md')):
            original = path.read_text()
            self.mutate_bundle(lambda d: d['initializations'][0]['strategy_paths'].__setitem__(0, bad))
            with self.assertRaises(ValueError):
                InitializationBundle(self.root, path)
            path.write_text(original)
        strategy = self.output / 'strategies/alpha.md'
        strategy.unlink()
        strategy.symlink_to(self.source / 'strategies/alpha.md')
        with self.assertRaisesRegex(ValueError, 'inside this project'):
            InitializationBundle(self.root, path)

    def test_source_route_id_path_order_and_first_attempt_are_required(self):
        path = self.source / 'samples/016.json'
        original = path.read_text()
        mutations = [lambda d: d['strategy_ids'].reverse(),
                     lambda d: d['strategy_ids'].__setitem__(1, 'alpha'),
                     lambda d: d['history'].pop(0),
                     lambda d: d.update(sample_index=0)]
        for mutation in mutations:
            data = json.loads(original)
            mutation(data)
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                self.build()
            self.assertFalse(self.output.exists())

    def test_selected_dataset_duplicates_and_source_order_rejected(self):
        for cases in ([self.cases[0], self.cases[0]], list(reversed(self.cases))):
            self.cases_path.write_text(''.join(json.dumps(c) + '\n' for c in cases))
            with self.assertRaises(ValueError):
                self.build()
            self.assertFalse(self.output.exists())

    def test_preparation_preserves_originals_and_refuses_overwrite(self):
        before = {path: sha256_bytes(path.read_bytes()) for path in self.source.rglob('*') if path.is_file()}
        path = self.build()
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(before, {path: sha256_bytes(path.read_bytes()) for path in before})


if __name__ == '__main__':
    unittest.main()
