"""Offline compatibility of verified fixed cold-start members and A2 views."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest

from src.engine.config import ROOT
from src.attacker.initialization import route_digest
from src.attacker.piminer import GenerationError


def adapter_class():
    path = os.environ.get('ALIGNED_COLD_START_MODULE_PATH')
    if not path:
        from src.attacker.aligned import PIMinerAlignedAdapter
        return PIMinerAlignedAdapter
    spec = importlib.util.spec_from_file_location('src.attacker._isolated_cold_start', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PIMinerAlignedAdapter


class AlignedA2ColdStartTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [json.loads(line) for line in
            (ROOT / 'data/agentdojo_full_observable_20261030/cases.jsonl').read_text().splitlines() if line.strip()]
        self.by_id = {task['case_id']: task for task in self.tasks}
        self.manifest = 'data/piminer_aligned_20261030/initializations.json'
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)

    def adapter(self, manifest=None):
        def no_online_session(**kwargs):
            self.fail('Offline route assembly must never create a model session')
        return adapter_class()(ROOT, 'offline', 'offline', manifest or self.manifest,
            revision_profile='calibrated', session_factory=no_online_session,
            a2_library='strategy_library_a2/library.json',
            a2_retrieval='data/a2/aligned22_20261030.field_v2.retrieval.json')

    def initial(self, attacker, cid='c016'):
        return attacker.generate_with_history(self.by_id[cid], None, None, (), 'P0', ())

    def variant_manifest(self, *, template_only=False, unknown=False, copied_template=False):
        data = json.loads((ROOT / self.manifest).read_text())
        entry = next(e for e in data['initializations'] if e['case_id'] == 'c016')
        if template_only:
            index = entry['strategy_ids'].index('_template_cold_start')
            for key in ('strategy_ids', 'strategy_paths', 'strategy_sha256'):
                entry[key] = [entry[key][index]]
        copied = None
        if copied_template:
            index = entry['strategy_ids'].index('_template_cold_start')
            copied = Path(self.temp.name) / '_template_cold_start.md'
            copied.write_bytes((ROOT / entry['strategy_paths'][index]).read_bytes())
            entry['strategy_paths'][index] = str(copied.relative_to(ROOT))
        if unknown:
            index = 0
            entry['strategy_ids'][index] = '_unknown_template'
            path = Path(self.temp.name) / '_unknown_template.md'
            path.write_text('An unknown strategy must not silently become a template.')
            entry['strategy_paths'][index] = str(path.relative_to(ROOT))
            entry['strategy_sha256'][index] = hashlib.sha256(path.read_bytes()).hexdigest()
        entry['route_sha256'] = route_digest(entry['strategy_ids'], entry['strategy_paths'], entry['strategy_sha256'])
        manifest = Path(self.temp.name) / 'initializations.json'
        manifest.write_text(json.dumps(data))
        return manifest, copied

    def test_real_mixed_route_keeps_order_template_bytes_and_real_selections(self):
        attacker = self.adapter()
        payload, record = self.initial(attacker)
        initial = attacker.bundle.for_case(self.by_id['c016'])
        document = attacker.routes['c016']['document']
        headers = re.findall(r'^===== Candidate strategy (\d+/\d+): (.*?) =====$', document, re.M)
        self.assertEqual(headers, [(f'{i}/3', sid) for i, sid in enumerate(initial['strategy_ids'], 1)])
        template = (ROOT / initial['strategy_paths'][-1]).read_bytes().decode('utf-8')
        self.assertTrue(document.endswith(template))
        metadata = record['routing']['a2']
        self.assertEqual(metadata['template_fallbacks'], ['_template_cold_start'])
        self.assertEqual(metadata['reason'], 'no_library_examples')
        self.assertEqual(set(metadata['selections']), set(initial['strategy_ids']) - {'_template_cold_start'})
        self.assertEqual(metadata['a2_document_chars'], len(document))
        self.assertEqual(metadata['strategy_document_sha256'], hashlib.sha256(document.encode()).hexdigest())
        self.assertEqual(payload.text, initial['payload'])
        self.assertEqual(record['calls'], [])
        for sid, selection in metadata['selections'].items():
            self.assertIn(attacker.resources.strategies[sid]['method'], document)
            for cid in selection['case_ids']:
                self.assertIn(attacker.resources.by_id[cid]['raw'], document)

    def test_template_only_route_reports_no_retrieval_hits(self):
        manifest, _ = self.variant_manifest(template_only=True)
        attacker = self.adapter(manifest)
        _, record = self.initial(attacker)
        self.assertEqual(record['routing']['a2']['selections'], {})
        self.assertEqual(record['routing']['a2']['template_fallbacks'], ['_template_cold_start'])
        self.assertEqual(attacker.routes['c016']['document'], attacker.bundle.strategy_document(self.by_id['c016']))

    def test_other_unknown_strategy_is_rejected(self):
        manifest, _ = self.variant_manifest(unknown=True)
        with self.assertRaises(GenerationError) as caught:
            self.initial(self.adapter(manifest))
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertIn('Unknown fixed A2 strategy', str(caught.exception.__cause__))

    def test_template_changed_after_bundle_validation_is_rejected(self):
        manifest, copied = self.variant_manifest(copied_template=True)
        attacker = self.adapter(manifest)
        copied.write_text(copied.read_text() + '\nchanged after bundle load\n')
        with self.assertRaises(GenerationError) as caught:
            self.initial(attacker)
        self.assertIn('Initialization input bytes changed', str(caught.exception.__cause__))

    def test_normal_route_is_exactly_the_existing_a2_composition(self):
        attacker = self.adapter()
        _, record = self.initial(attacker, 'c000')
        initial = attacker.bundle.for_case(self.by_id['c000'])
        strategies = [dict(id=sid, path=str(ROOT / attacker.resources.strategies[sid]['source']))
                      for sid in initial['strategy_ids']]
        expected_document, expected_metadata = attacker.resources.compose(self.by_id['c000'], strategies)
        self.assertEqual(attacker.routes['c000']['document'], expected_document)
        self.assertEqual(record['routing']['a2'], expected_metadata)

    def test_all_22_fixed_initial_payloads_assemble_without_online_calls(self):
        attacker = self.adapter()
        for task in self.tasks:
            with self.subTest(case_id=task['case_id']):
                payload, record = self.initial(attacker, task['case_id'])
                self.assertEqual(record['status'], 'ready')
                self.assertEqual(record['calls'], [])
                self.assertEqual(payload.text, attacker.bundle.for_case(task)['payload'])


if __name__ == '__main__':
    unittest.main()
