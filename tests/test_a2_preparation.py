"""Offline checks for A2 data fingerprints and legacy resource compatibility."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_a2
from src.attacker.a2 import A2Resources
from src.engine.config import ROOT, load_cases, load_config


class A2DataFingerprintTests(unittest.TestCase):
    def test_input_hashes_skip_python_without_reading_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resource = root / 'strategy.md'
            resource.write_text('data resource')
            with patch.object(prepare_a2, 'ROOT', root):
                result = prepare_a2.input_hashes([root / 'missing.py', resource])
        self.assertEqual(result, {'strategy.md': hashlib.sha256(b'data resource').hexdigest()})

    def test_provenance_keeps_data_checks_without_reading_source_hashes(self):
        original_hash = prepare_a2.file_hash
        seen = []
        def data_hash(path):
            self.assertNotEqual(Path(path).suffix, '.py')
            seen.append(Path(path))
            return original_hash(path)
        with patch.object(prepare_a2, 'file_hash', side_effect=data_hash):
            provenance, dependencies, annotations = prepare_a2.verify_provenance()
        self.assertTrue(annotations['labels'])
        self.assertTrue(seen)
        self.assertTrue(all(path.suffix != '.py' for path in dependencies))
        self.assertTrue(all('sha256' not in entry
                            for entry in provenance['upstream_files'].values()
                            if Path(entry['snapshot']).suffix == '.py'))
        self.assertTrue(all('sha256' not in entry for entry in provenance['copied_functions'].values()))
        self.assertIn('sha256', provenance['source_annotations'])

    def test_annotation_fingerprint_remains_enforced(self):
        original_read = prepare_a2.read
        def changed_provenance(path):
            value = original_read(path)
            if Path(path).name == 'provenance.json':
                value['source_annotations']['sha256'] = 'changed-data'
            return value
        with patch.object(prepare_a2, 'read', side_effect=changed_provenance):
            with self.assertRaisesRegex(ValueError, 'reviewed annotations changed'):
                prepare_a2.verify_provenance()

    def test_new_library_excludes_code_fingerprints_and_matches_legacy_data(self):
        library = prepare_a2.build_library(ROOT / 'strategy_library_a2/case_annotations.json')
        self.assertFalse(any(Path(path).suffix == '.py' for path in library['inputs_sha256']))
        old = json.loads((ROOT / 'strategy_library_a2/library.json').read_text())
        before = deepcopy(old)
        self.assertEqual(prepare_a2.data_only_library(old), library)
        self.assertEqual(old, before)
        self.assertTrue(library['source_sha256'])
        changed = deepcopy(old)
        changed['cases'][0]['raw'] += ' changed data'
        self.assertNotEqual(prepare_a2.data_only_library(changed), library)

    def test_runtime_ignores_legacy_code_fingerprints_but_checks_data(self):
        library = json.loads((ROOT / 'strategy_library_a2/library.json').read_text())
        retrieval = json.loads((ROOT / 'data/a2/dev.retrieval.json').read_text())
        for resource in (library, retrieval):
            for path in list(resource['inputs_sha256']):
                if Path(path).suffix == '.py':
                    resource['inputs_sha256'][path] = 'obsolete-source-digest'
            resource['inputs_sha256']['src/no-longer-present.py'] = 'obsolete-source-digest'
        with tempfile.TemporaryDirectory(prefix='a2-data-hashes-', dir=ROOT) as directory:
            root = Path(directory)
            library_path, retrieval_path = root / 'library.json', root / 'retrieval.json'
            library_path.write_text(json.dumps(library))
            retrieval['library_sha256'] = hashlib.sha256(library_path.read_bytes()).hexdigest()
            retrieval_path.write_text(json.dumps(retrieval))
            resources = A2Resources(ROOT, library_path, retrieval_path)
            self.assertTrue(all(path.suffix != '.py' for path in resources.input_paths))
            case = load_cases(load_config(ROOT / 'experiments/a2_native_dev.yaml'))[0]
            resources.validate_case(case)
            retrieval['inputs_sha256']['data/dev.jsonl'] = 'changed-data'
            retrieval_path.write_text(json.dumps(retrieval))
            with self.assertRaisesRegex(ValueError, 'prepared input changed'):
                A2Resources(ROOT, library_path, retrieval_path)

    def test_reuse_library_preserves_old_bytes_with_data_only_new_retrieval(self):
        library_path = ROOT / 'strategy_library_a2/library.json'
        before = library_path.read_bytes()
        def offline_embeddings(library, tasks, model):
            return ([{case['case_id']: 0.0 for case in library['cases']} for _ in tasks],
                    {'offline_fixture': True})
        with tempfile.TemporaryDirectory(prefix='a2-data-reuse-', dir=ROOT) as directory:
            retrieval_path = Path(directory) / 'retrieval.json'
            args = SimpleNamespace(dataset=ROOT / 'data/dev.jsonl', library=library_path,
                retrieval=retrieval_path, case_annotations=ROOT / 'strategy_library_a2/case_annotations.json',
                task_annotations=ROOT / 'data/a2/dev.annotations.json', model=Path('unused'),
                reuse_library=True)
            with patch.object(prepare_a2, 'embed', side_effect=offline_embeddings):
                result = prepare_a2.prepare(args)
            retrieval = json.loads(retrieval_path.read_text())
            self.assertFalse(any(Path(path).suffix == '.py' for path in retrieval['inputs_sha256']))
            self.assertEqual(retrieval['library_sha256'], hashlib.sha256(before).hexdigest())
            self.assertEqual(result['library_sha256'], hashlib.sha256(before).hexdigest())
            resources = A2Resources(ROOT, library_path, retrieval_path)
            resources.validate_case(load_cases(load_config(ROOT / 'experiments/a2_native_dev.yaml'))[0])
        self.assertEqual(library_path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
