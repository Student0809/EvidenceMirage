"""Offline negative controls for terminal-source and initial-execution auditing."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.verify_piminer_terminal30_20261031 import (
    ROOT, read, result_rows, stratify, verify_entry, verify_initial_execution)


class TerminalSourceAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = read(ROOT/'data/piminer_terminal30_20261031/reference.json')
        cls.bundle = read(ROOT/cls.reference['runtime_manifest'])
        cls.base = read(ROOT/cls.reference['base_manifest'])
        cls.cases = [json.loads(line) for line in (ROOT/cls.reference['cases_path']).read_text().splitlines() if line]

    def fixture(self):
        source = deepcopy(self.reference['sources'][0])
        return (deepcopy(self.cases[0]), deepcopy(self.base['initializations'][0]),
                deepcopy(self.bundle['initializations'][0]), source, read(source['sample_path']))

    def test_saved_terminal_is_evaluated_and_analysis_is_empty(self):
        row = verify_entry(*self.fixture())
        self.assertTrue(row['analysis_empty'])
        self.assertTrue(row['payload_changed_from_original_p0'])
        self.assertEqual(row['source_terminal_iter'], 4)

    def test_original_p0_cannot_replace_terminal_payload(self):
        values = self.fixture()
        values[2]['payload'] = values[1]['payload']
        with self.assertRaisesRegex(ValueError, 'not_source_terminal_payload'):
            verify_entry(*values)

    def test_source_analysis_cannot_enter_runtime(self):
        values = self.fixture()
        values[2]['analysis'] = values[4]['history'][-1]['analysis']
        with self.assertRaisesRegex(ValueError, 'historical_analysis_imported'):
            verify_entry(*values)

    def test_an_earlier_source_iteration_cannot_be_called_terminal(self):
        values = self.fixture()
        values[3]['source_terminal_iter'] -= 1
        with self.assertRaisesRegex(ValueError, 'source_terminal_position'):
            verify_entry(*values)

    def test_case_identity_cannot_be_remapped(self):
        values = self.fixture()
        values[4]['user_task_id'] = 'different-source-task'
        with self.assertRaisesRegex(ValueError, 'source_public_identity'):
            verify_entry(*values)

    def test_historical_outcome_cannot_be_relabelled(self):
        values = self.fixture()
        values[3]['source_status'] = 'hit'
        with self.assertRaisesRegex(ValueError, 'source_status'):
            verify_entry(*values)

    def test_route_cannot_change_with_terminal_payload(self):
        values = self.fixture()
        values[2]['strategy_ids'] = list(reversed(values[2]['strategy_ids']))
        with self.assertRaisesRegex(ValueError, 'route_or_metadata_changed'):
            verify_entry(*values)

    def test_new_initial_execution_must_use_terminal_digest(self):
        source = self.reference['sources'][0]
        entry = self.bundle['initializations'][0]
        with tempfile.TemporaryDirectory() as temp:
            case_dir = Path(temp)/'c000'
            case_dir.mkdir()
            record = dict(payload=entry['payload'], payload_sha256=entry['payload_sha256'], analysis='',
                          calls=[], initialization='historical_route_and_p0', reused_target_feedback=False)
            (case_dir/'initial.json').write_text(json.dumps(record))
            events = [dict(event='GENERATION_COMPLETED', generation_id='c000/shared/P0', raw_path='initial.json'),
                      dict(event='TARGET_STARTED', episode_id='c000/shared/000', payload_sha256='wrong')]
            (case_dir/'events.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in events))
            with self.assertRaisesRegex(ValueError, 'initial_target_not_fresh_source_payload'):
                verify_initial_execution(Path(temp), source)

    def test_absent_new_target_is_pending_not_reproduced(self):
        with tempfile.TemporaryDirectory() as temp:
            result = verify_initial_execution(Path(temp), self.reference['sources'][0])
            self.assertEqual(result['status'], 'pending')
            self.assertIsNone(result['initial_target_gold'])
            with self.assertRaisesRegex(ValueError, 'missing_initial_import'):
                verify_initial_execution(Path(temp), self.reference['sources'][0], complete=True)

    def test_strata_separate_replay_revision_and_unresolved(self):
        source = [dict(case_id='c000', source_status='hit'), dict(case_id='c001', source_status='miss'),
                  dict(case_id='c002', source_status='hit')]
        native = [dict(case_id='c000', gold_success=True, timeline=[dict(gold=True, action='Initial')]),
                  dict(case_id='c001', gold_success=True, timeline=[dict(gold=False, action='Initial'),
                                                                 dict(gold=True, action='Revise')]),
                  dict(case_id='c002', gold_success=None, timeline=[dict(gold=None, action='Initial')])]
        result = stratify(source, native)
        self.assertEqual(result['hit']['initial_replay_successes'], 1)
        self.assertEqual(result['hit']['unresolved_or_error'], 1)
        self.assertEqual(result['hit']['complete_failures'], 0)
        self.assertEqual(result['miss']['initial_replay_successes'], 0)
        self.assertEqual(result['miss']['later_revise_successes'], 1)

    def test_success_attribution_reads_generated_revision_path(self):
        for action, payload_id, path in (('Retry', 'P0', 'imported_terminal_payload'),
                                        ('Revise', 'P1', 'grounded_false'),
                                        ('Revise', 'P1', 'piminer_black_box'),
                                        ('Retry', 'P1', 'grounded_false')):
            with self.subTest(action=action, path=path), tempfile.TemporaryDirectory() as temp:
                case_dir = Path(temp)/'c000'
                case_dir.mkdir()
                eid = 'c000/stage_aware/001'
                (case_dir/'episodes.jsonl').write_text(json.dumps(dict(episode_id=eid, payload_id=payload_id))+'\n')
                if payload_id != 'P0':
                    generation = case_dir/'raw/c000/stage_aware/P1.generation.json'
                    generation.parent.mkdir(parents=True)
                    generation.write_text(json.dumps(dict(revision_path=path)))
                timeline = [dict(gold=False, action='Initial', status='VALID_GOLD_FALSE', attempt=1),
                            dict(gold=True, action=action, status='VALID_GOLD_TRUE', attempt=2, episode_id=eid)]
                native = dict(case_id='c000', timeline=timeline, target_started=2, target_completed=2,
                              gold_success=True, disposition='confirmed_success', terminal_error=None)
                result = result_rows(Path(temp), [self.reference['sources'][0]], [native])
                self.assertEqual(result[0]['first_success_revision_path'], path)
                self.assertEqual(result[0]['first_success_action'], action)
                self.assertEqual(result[0]['immediate_action'], action)
                self.assertEqual(result[0]['successful_payload_origin'],
                                 'imported_terminal' if payload_id == 'P0' else path)
                self.assertEqual(result[0]['source_recorded_attempts'], 5)

    def test_retry_of_f_candidate_keeps_f_origin_separate_from_retry_action(self):
        source = [dict(case_id='c000', source_status='miss')]
        native = [dict(case_id='c000', gold_success=True, successful_payload_origin='grounded_false',
                       first_success_revision_path='grounded_false',
                       timeline=[dict(gold=False, action='Initial'), dict(gold=False, action='Revise'),
                                 dict(gold=True, action='Retry')])]
        result = stratify(source, native)['miss']
        self.assertEqual(result['later_retry_successes'], 1)
        self.assertEqual(result['later_revise_successes'], 0)
        self.assertEqual(result['first_success_immediate_actions'], {'Retry': 1})
        self.assertEqual(result['successful_payload_origins'], {'grounded_false': 1})


if __name__ == '__main__':
    unittest.main()
