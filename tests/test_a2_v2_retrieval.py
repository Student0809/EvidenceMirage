"""Offline retrieval invariants: preservation, diversity, and slot visibility."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from src.attacker.retrieval import build_embedding_fields, select_cases_v2
from src.attacker.a2 import A2Resources
from src.engine.config import ROOT
from scripts.prepare_a2_v2 import rebuild_retrieval, resolve_source_context


class CharacterTokenizer:
    """Exact offset fixture; no external packages or model downloads."""
    def __call__(self, text, add_special_tokens=True, return_offsets_mapping=False, **kwargs):
        value = {'input_ids': list(range(len(text) + (2 if add_special_tokens else 0)))}
        if return_offsets_mapping:
            value['offset_mapping'] = [(i, i + 1) for i in range(len(text))]
        return value


class A2V2RetrievalTests(unittest.TestCase):
    def case(self, cid, *, scenario='workspace', kind='file_operation', group=None, situation=None):
        return dict(case_id=cid, strategy_id='strategy', scenario=scenario, task_type=kind,
                    group=group or ['agentdojo', scenario, cid], situation=situation or cid,
                    raw='unaltered ' + cid, outcome_label='unknown')

    def select(self, cases, scores):
        return select_cases_v2(cases, strategy_id='strategy', scenario='workspace',
            task_type='file_operation', current_group=['agentdojo', 'workspace', 'current'], scores=scores)

    def test_single_precise_match_survives_fallback_even_with_lower_similarity(self):
        cases = [self.case('precise'), self.case('cross', scenario='slack'),
                 self.case('wrong', kind='other')]
        before = deepcopy(cases)
        chosen, log = self.select(cases, {'precise': .1, 'cross': .8, 'wrong': .9})
        self.assertEqual([c['case_id'] for c in chosen], ['precise', 'cross'])
        self.assertEqual(log['selected_tiers']['precise'], 'same_scenario_and_type')
        self.assertEqual(cases, before)

    def test_duplicate_groups_and_normalized_situations_do_not_consume_slots(self):
        cases = [self.case('a', situation='Same  task'), self.case('b', group=['agentdojo', 'workspace', 'a']),
                 self.case('c', situation='same\nTASK'), self.case('d', scenario='slack')]
        chosen, log = self.select(cases, {'a': .9, 'b': .8, 'c': .7, 'd': .1})
        self.assertEqual([c['case_id'] for c in chosen], ['a', 'd'])
        self.assertEqual(log['duplicates_skipped'], ['b', 'c'])

    def test_current_group_excluded_and_shortfall_is_not_padded(self):
        cases = [self.case('current'), self.case('only')]
        chosen, log = self.select(cases, {'current': 1, 'only': .1})
        self.assertEqual([c['case_id'] for c in chosen], ['only'])
        self.assertEqual(log['shortfall'], 1)

    def test_late_multiple_slots_survive_field_budget(self):
        context = 'x' * 900 + '{INJECTION:first}' + 'y' * 900 + '{INJECTION:second}' + 'z' * 900
        fields, audit = build_embedding_fields('find the document', context, CharacterTokenizer(),
                                               task_budget=32, context_budget=100)
        self.assertEqual(len(fields['context']), 2)
        for marker in ('{INJECTION:first}', '{INJECTION:second}'):
            self.assertTrue(any(marker in part for part in fields['context']))
        self.assertTrue(all(len(part) + 2 <= 100 for part in fields['context']))
        self.assertEqual(audit['context_marker_count'], audit['retained_marker_count'])
        self.assertFalse(audit['legacy_all_markers_retained'])

    def test_no_marker_context_retains_both_ends_with_budget(self):
        fields, audit = build_embedding_fields('task', 'start' + 'x' * 900 + 'finish',
                                               CharacterTokenizer(), context_budget=100)
        self.assertTrue(fields['context'][0].startswith('start'))
        self.assertTrue(fields['context'][0].endswith('finish'))
        self.assertLessEqual(len(fields['context'][0]) + 2, 100)
        self.assertEqual(audit['context_marker_count'], 0)

    def test_oversized_marker_rejected_instead_of_silently_truncated(self):
        with self.assertRaisesRegex(ValueError, 'marker'):
            build_embedding_fields('task', '{INJECTION:' + 'x' * 100 + '}',
                                   CharacterTokenizer(), context_budget=20)

    def test_selector_ablation_preserves_scores_and_runtime_replays_v2(self):
        baseline = json.loads((ROOT / 'data/a2/dev.retrieval.json').read_text())
        library = json.loads((ROOT / 'strategy_library_a2/library.json').read_text())
        tasks = [json.loads(line) for line in (ROOT / baseline['dataset_path']).read_text().splitlines() if line.strip()]
        before = deepcopy(baseline)
        result = rebuild_retrieval(baseline, library, tasks, variant='selector_only')
        self.assertEqual(baseline, before)
        self.assertEqual(result['embedding'], baseline['embedding'])
        for cid, record in result['tasks'].items():
            self.assertEqual(record['scores'], baseline['tasks'][cid]['scores'])
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'retrieval.json'
            path.write_text(json.dumps(result))
            resource = A2Resources(ROOT, 'strategy_library_a2/library.json', path)
            for task in tasks:
                resource.validate_case(task)
            cid = tasks[0]['case_id']
            sid = next(iter(result['tasks'][cid]['selections']))
            result['tasks'][cid]['selections'][sid]['case_ids'] = []
            path.write_text(json.dumps(result))
            resource = A2Resources(ROOT, 'strategy_library_a2/library.json', path)
            with self.assertRaisesRegex(ValueError, 'selection cache'):
                resource.validate_case(tasks[0])

    def test_archived_context_links_resolve_with_checked_provenance(self):
        library = json.loads((ROOT / 'strategy_library_a2/library.json').read_text())
        cases = [case for case in library['cases'] if 'strategy_library/contexts/' in case['situation']]
        self.assertTrue(cases)
        for case in cases:
            context, audit, path = resolve_source_context(case)
            self.assertTrue(context)
            self.assertTrue(path.is_file())
            self.assertEqual(audit['context_resolution'], 'linked_current_dataset')
        wrong = deepcopy(cases[0])
        wrong['group'][-1] = 'different_task'
        with self.assertRaisesRegex(ValueError, 'group mismatch'):
            resolve_source_context(wrong)

    def test_context_reference_cannot_escape_library_directory(self):
        case = self.case('a')
        case.update(user_task='task', situation='task\n\nstrategy_library/contexts/../../outside.json')
        with self.assertRaisesRegex(ValueError, 'escapes'):
            resolve_source_context(case)


if __name__ == '__main__':
    unittest.main()
