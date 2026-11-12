"""Rolling experience and original delta semantics, entirely offline."""
import ast
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
import fcntl
import inspect
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.attacker.experience import RollingExperience, ROLLING_CUTOFF
from src.vendor import delta, experience


ROOT = Path(__file__).resolve().parents[1]


def original_helpers(filename, names):
    source = (ROOT / 'data/original/piminer_code' / filename).read_text()
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = dict(json=json, fcntl=fcntl, Path=Path, Dict=dict, Any=object, List=list,
                     INTRA_EXPERIENCE_FILE=experience.INTRA_EXPERIENCE_FILE)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec'), namespace)
    return namespace


def inputs(index, success=False, policy='stage_aware'):
    case = dict(case_id=f'c{index:03d}', suite_name='workspace', user_task_id=f'user_task_{index}',
        injection_task_id='injection_task_3', user_task_prompt=f'Public task {index}',
        injection_task_goal='Public goal', context_with_placeholder='before {INJECTION:slot} after')
    history = [dict(iter=0, analysis='First analysis', injection=f'Candidate {index} α',
                    security=False, utility=True, final_output='Public final 0'),
               dict(iter=1, analysis='Final analysis', injection=f'Final candidate {index}',
                    security=success, utility=False, final_output='Public final 1')]
    outcome = dict(case_id=case['case_id'], policy_id=policy, gold_success=success,
        status='VALID_GOLD_TRUE' if success else 'VALID_GOLD_FALSE', target_attempts=1)
    return case, history, outcome, ['strategy-a', 'strategy-b']


def publish_worker(directory, index):
    store = RollingExperience(directory)
    added = store.publish(*inputs(index, success=index % 2 == 0))
    _, meta = store.snapshot('c999')
    return added, meta['entries'], len(set(meta['case_ids']))


class RollingExperienceTests(unittest.TestCase):
    def test_vendor_functions_are_verbatim_and_delta_matches_original(self):
        for filename, module in [('iterative_attack_claude_code.py', experience),
                                 ('iterative_attack_common.py', delta)]:
            source = (ROOT / 'data/original/piminer_code' / filename).read_text()
            originals = {node.name: ast.get_source_segment(source, node)
                         for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
            for name, value in vars(module).items():
                if inspect.isfunction(value) and value.__module__ == module.__name__:
                    self.assertEqual(inspect.getsource(value).rstrip(), originals[name])
        original = original_helpers('iterative_attack_common.py', ['_format_attempt', 'build_delta_block'])
        case, history, _, _ = inputs(0)
        for count in (1, 2):
            for memory in ('', ' Refreshed public experience \n'):
                actual = delta.build_delta_block(case, history[:count], 'black_box', memory)
                expected = original['build_delta_block'](case, history[:count], 'black_box', memory)
                self.assertEqual(actual, expected)
                if count == 2:
                    self.assertNotIn('Public final 0', actual)
                    self.assertIn('Public final 1', actual)

    def test_summary_schema_and_rendering_match_original(self):
        reference = original_helpers('iterative_attack_claude_code.py', [
            '_intra_experience_path', '_clip_context', '_append_intra_experience',
            '_format_intra_experience_block'])
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            store = RollingExperience(directory/'new')
            old = directory/'old'
            old.mkdir()
            for index in (7, 2, 4):
                case, history, outcome, strategies = inputs(index, success=index == 4)
                self.assertTrue(store.publish(case, history, outcome, strategies))
                sample = dict(case, sample_index=index, history=history,
                    status='hit' if outcome['gold_success'] else 'miss', strategy_ids=strategies)
                reference['_append_intra_experience'](old, sample)
            actual = [json.loads(line) for line in store.path.read_text().splitlines()]
            expected = [json.loads(line) for line in (old/experience.INTRA_EXPERIENCE_FILE).read_text().splitlines()]
            self.assertEqual([{k: v for k, v in entry.items() if not k.startswith('_')}
                              for entry in actual], expected)
            text, metadata = store.snapshot('c999')
            self.assertEqual(text, reference['_format_intra_experience_block'](old, ROLLING_CUTOFF))
            self.assertEqual(metadata['case_ids'], ['c002', 'c004', 'c007'])
            self.assertEqual(metadata['entries'], 3)
            self.assertEqual(metadata['before_index'], ROLLING_CUTOFF)
            self.assertEqual(metadata['max_chars'], 20000)
            self.assertEqual(metadata['source'], str(store.path))

    def test_soft_budget_preserves_one_oversize_latest_entry(self):
        reference = original_helpers('iterative_attack_claude_code.py', [
            '_intra_experience_path', '_format_intra_experience_block'])
        with tempfile.TemporaryDirectory() as directory:
            store = RollingExperience(directory)
            store.publish(*inputs(1))
            case, history, outcome, strategies = inputs(2)
            history[-1]['injection'] = 'x' * 21000
            store.publish(case, history, outcome, strategies)
            text, metadata = store.snapshot('c999')
            self.assertGreater(len(text), 20000)
            self.assertIn('older experience truncated to fit 20000 chars', text)
            self.assertNotIn('### Sample 1 ', text)
            self.assertIn('### Sample 2 ', text)
            self.assertEqual(text, reference['_format_intra_experience_block'](Path(directory), ROLLING_CUTOFF))
            self.assertEqual(metadata['entries'], 2)

    def test_empty_current_future_partial_other_policy_and_other_run_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = RollingExperience(root/'prior_run'/'stage_aware')
            prior.publish(*inputs(3))
            other = RollingExperience(root/'current_run'/'always_revise')
            other.publish(*inputs(4, policy='always_revise'))
            store = RollingExperience(root/'current_run'/'stage_aware')
            text, metadata = store.snapshot('c000')
            self.assertEqual(text, '')
            self.assertEqual(metadata['entries'], 0)
            # Uncommitted temporary files and future case state are never read.
            (store.directory/'.experience-orphan.tmp').write_text(prior.path.read_text())
            (store.directory/'future_case.json').write_text(json.dumps(dict(status='running', case_id='c005')))
            case, history, outcome, strategies = inputs(5)
            self.assertFalse(store.publish(case, history, dict(outcome, status='running'), strategies))
            self.assertEqual(store.snapshot('c000')[0], '')
            # Rolling includes already-terminal higher indices, unlike a prefix schedule.
            store.publish(*inputs(9))
            text, metadata = store.snapshot('c000')
            self.assertEqual(metadata['case_ids'], ['c009'])
            self.assertIn('Public task 9', text)
            self.assertNotIn('Public task 3', text)
            self.assertNotIn('Public task 4', text)
            self.assertNotIn('Public task 5', text)
            self.assertEqual(store.snapshot('c009')[0], '')
            self.assertEqual(store.snapshot('c009')[1]['entries'], 0)
            with self.assertRaisesRegex(ValueError, 'another policy'):
                store.publish(*inputs(8, policy='always_revise'))

    def test_invalid_or_error_outcomes_are_never_published(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RollingExperience(directory)
            case, history, outcome, strategies = inputs(0)
            for status, gold in [('TARGET_ERROR', None), ('GENERATION_ERROR', None),
                                 ('running', False), ('VALID_GOLD_FALSE', None),
                                 ('VALID_GOLD_TRUE', False), ('VALID_GOLD_FALSE', 0)]:
                self.assertFalse(store.publish(case, history, dict(outcome, status=status, gold_success=gold), strategies))
            incomplete = deepcopy(history)
            incomplete[-1]['security'] = None
            self.assertFalse(store.publish(case, incomplete, outcome, strategies))
            self.assertFalse(store.publish(case, [], outcome, strategies))
            with self.assertRaisesRegex(ValueError, 'outcome does not match'):
                store.publish(case, history, dict(outcome, status='VALID_GOLD_TRUE', gold_success=True), strategies)
            with self.assertRaisesRegex(ValueError, 'budget'):
                store.publish(case, history, dict(outcome, target_attempts=5), strategies)
            self.assertEqual(store.snapshot('c999')[1]['entries'], 0)

    def test_public_summary_whitelist_and_explicit_source_index(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RollingExperience(directory)
            case, history, outcome, strategies = inputs(6)
            case['case_id'] = outcome['case_id'] = 'source_six'
            case['provenance'] = {'source_metadata': {'source_sample_index': 6}}
            case['private_state'] = 'DO_NOT_PUBLISH'
            for entry in history:
                entry['trajectory'] = 'DO_NOT_PUBLISH'
                entry['evidence'] = 'DO_NOT_PUBLISH'
            self.assertTrue(store.publish(case, history, outcome, strategies))
            text, metadata = store.snapshot('c999')
            self.assertIn('### Sample 6 ', text)
            self.assertEqual(metadata['case_ids'], ['source_six'])
            self.assertNotIn('DO_NOT_PUBLISH', text)
            self.assertNotIn('DO_NOT_PUBLISH', store.path.read_text())
            bad_case, h, o, s = inputs(7)
            bad_case['sample_index'] = 8
            with self.assertRaisesRegex(ValueError, 'conflicting'):
                store.publish(bad_case, h, o, s)

    def test_idempotence_and_same_index_different_identity_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RollingExperience(directory)
            args = inputs(3)
            self.assertTrue(store.publish(*args))
            before = store.path.read_bytes()
            self.assertFalse(store.publish(*args))
            self.assertEqual(store.path.read_bytes(), before)
            case, history, outcome, strategies = inputs(3)
            case.update(case_id='alias_three', sample_index=3)
            outcome['case_id'] = case['case_id']
            with self.assertRaisesRegex(ValueError, 'another case'):
                store.publish(case, history, outcome, strategies)

    def test_failed_atomic_replace_preserves_old_snapshot_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RollingExperience(directory)
            store.publish(*inputs(0))
            before = store.snapshot('c999')
            with patch('src.attacker.experience.os.replace', side_effect=OSError('simulated interrupted commit')):
                with self.assertRaises(OSError):
                    store.publish(*inputs(1))
            self.assertEqual(store.snapshot('c999'), before)
            self.assertFalse(list(Path(directory).glob('.experience-*.tmp')))
            self.assertTrue(store.publish(*inputs(1)))

    def test_partial_or_uncommitted_store_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RollingExperience(directory)
            store.path.write_text('{"sample_index":')
            with self.assertRaises(ValueError):
                store.snapshot('c999')
            with self.assertRaises(ValueError):
                store.publish(*inputs(0))
            store.path.write_text(json.dumps(dict(sample_index=1, outcome='pending'))+'\n')
            with self.assertRaisesRegex(ValueError, 'uncommitted'):
                store.snapshot('c999')

    def test_project_filesystem_supports_shared_snapshot_and_exclusive_publish(self):
        # /tmp can use local flock semantics while the project lives on NFS.
        # Exercise the real project mount with the same snapshot/publish paths
        # used by workers, and leave no test data in the benchmark run directory.
        with tempfile.TemporaryDirectory(prefix='experience-lock-test-', dir=ROOT/'runs') as directory:
            path = Path(directory)
            self.assertEqual(path.parent.resolve(), (ROOT/'runs').resolve())
            store = RollingExperience(path)
            self.assertEqual(store.snapshot('c999')[1]['entries'], 0)
            self.assertTrue(store.publish(*inputs(0)))
            self.assertEqual(store.snapshot('c999')[1]['case_ids'], ['c000'])
            self.assertFalse(store.publish(*inputs(0)))
            self.assertTrue(store.publish(*inputs(1)))
            self.assertEqual(store.snapshot('c999')[1]['case_ids'], ['c000', 'c001'])
        self.assertFalse(path.exists())

    def test_process_concurrency_deduplicates_inside_lock_and_snapshots_are_whole(self):
        with tempfile.TemporaryDirectory() as directory:
            # Repeated indices race across independent processes, including reads
            # while other writers commit. Exactly one publisher can win per case.
            with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context('spawn')) as pool:
                results = list(pool.map(publish_worker, [directory]*24, [i % 8 for i in range(24)]))
            self.assertEqual(sum(added for added, _, _ in results), 8)
            self.assertTrue(all(count == unique for _, count, unique in results))
            store = RollingExperience(directory)
            text, metadata = store.snapshot('c999')
            self.assertEqual(metadata['entries'], 8)
            self.assertEqual(metadata['case_ids'], [f'c{i:03d}' for i in range(8)])
            self.assertEqual(len(store.path.read_text().splitlines()), 8)
            for index in range(8):
                self.assertEqual(text.count(f'### Sample {index} '), 1)


if __name__ == '__main__':
    unittest.main()
