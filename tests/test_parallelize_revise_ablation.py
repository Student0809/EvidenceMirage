"""Offline claims, parallel barriers, source isolation and reconciliation checks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from scripts.parallelize_revise_ablation_20261030 import claim_case, execute_parallel_case, reconcile
from scripts.run_revise_ablation_20261030 import ARMS, audit_source, canonical_hash
from src.engine.config import environment_versions
from src.engine.storage import write_json
from src.schema import Payload, TargetResult
from tests.test_revise_ablation import source_fixture


class ParallelRevisionTests(unittest.TestCase):
    def test_exclusive_claim_and_partial_claim_keep_first_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / 'c006.log').write_text('original owner')
            first = claim_case(run, 'c005', 'owner')
            second = claim_case(run, 'c006', 'owner')
            self.assertTrue(first['claimed'])
            self.assertFalse(second['claimed'])
            self.assertEqual((run / 'c006.log').read_text(), 'original owner')
            self.assertFalse(claim_case(run, 'c005', 'another')['claimed'])
            self.assertEqual(json.loads((run / 'c005/handoff_claim.json').read_text())['coordination_id'], 'owner')

    def test_parallel_generation_and_repeat_barriers_preserve_history(self):
        case, initial, episode, raw, generation = source_fixture()
        source = audit_source(case, initial, episode, raw, generation)
        lock, gen_barrier, target_barrier = threading.Lock(), threading.Barrier(4), threading.Barrier(4)
        histories, generated, active, repetitions = [], set(), set(), {a: 0 for a in ARMS}

        class Attacker:
            def __init__(self, arm):
                self.arm = arm

            def bind_run(self, path, policies):
                pass

            def begin_branch(self, case, arm):
                assert arm == self.arm

            def generate_with_history(self, case, payload, episode, packets, revision_id, history):
                if payload is None:
                    return Payload('P0', initial['payload']), deepcopy(generation)
                with lock:
                    histories.append(deepcopy(history))
                    generated.add(self.arm)
                gen_barrier.wait(timeout=10)
                return Payload('P1', 'synthetic-' + self.arm), dict(status='ready', calls=[], experience={'entries': 0})

        class Target:
            def __init__(self, arm):
                self.arm = arm

            def execute(self, case, payload, episode_id):
                with lock:
                    assert len(generated) == 4
                    assert self.arm not in active
                    repeat = int(episode_id.rsplit('/', 1)[1])
                    assert repetitions[self.arm] == repeat - 1
                    if repeat == 2:
                        assert all(count >= 1 for count in repetitions.values())
                    active.add(self.arm)
                target_barrier.wait(timeout=10)
                with lock:
                    active.remove(self.arm)
                    repetitions[self.arm] += 1
                return TargetResult(True, final='synthetic', utility=True)

        def factory(config):
            arm = next(iter(config['policies']))
            return Target(arm), Attacker(arm)

        plan = dict(kind='SYNTHETIC_TEST_ONLY', environment=environment_versions(), inputs_sha256={},
            cases=[case], case_ids=['c000'], sources={'c000': source}, seed=17,
            arm_configs={arm: dict(observation={}, policies={arm: {}}) for arm in ARMS})
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            self.assertTrue(claim_case(run, 'c000', 'owner')['claimed'])
            result = execute_parallel_case(plan, 'c000', run, 'owner', factory,
                lambda config: SimpleNamespace(collect=lambda *args: ()))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual({canonical_hash(h) for h in histories}, {source['history_sha256']})
        self.assertEqual(set(repetitions.values()), {2})
        self.assertFalse(active)
        self.assertTrue(all(a['physical_cost']['target_executions'] == 2 for a in result['arms'].values()))

    def test_reconciliation_reclassifies_only_claim_conflict_and_preserves_case_files(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            amendment = run / 'amendment'
            amendment.mkdir()
            write_json(run / 'manifest.json', dict(case_ids=['c005', 'c006']))
            write_json(run / 'results.json', [dict(case_id='c005', status='worker_error', error_type='FileExistsError'),
                dict(case_id='c006', status='worker_error', error_type='ActualInfrastructureError')])
            write_json(run / 'status.json', dict(status='completed_with_errors'))
            for cid in ('c005', 'c006'):
                write_json(run / cid / 'result.json', dict(case_id=cid, status='completed', arms={
                    arm: dict(trials=[dict(repeat=0, gold=False),
                        dict(repeat=1, gold=None, error='SyntheticTimeout', status='TARGET_ERROR')]) for arm in ARMS}))
            before = {cid: (run / cid / 'result.json').read_bytes() for cid in ('c005', 'c006')}
            reconcile(run, amendment, [dict(case_id='c005', claimed=True)])
            result = json.loads((run / 'results.json').read_text())
            self.assertEqual(result[0]['status'], 'completed')
            self.assertEqual(result[1]['status'], 'worker_error')
            self.assertEqual(json.loads((run / 'summary.json').read_text())['arms']['full_original']['missing_slots'], 2)
            self.assertTrue((amendment / 'original_parent_terminal/results.json').exists())
            self.assertEqual(before, {cid: (run / cid / 'result.json').read_bytes() for cid in before})

    def test_new_worker_terminal_error_is_not_hidden_by_expected_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            amendment = run / 'amendment'
            amendment.mkdir()
            write_json(run / 'manifest.json', dict(case_ids=['c005']))
            write_json(run / 'results.json', [dict(case_id='c005', status='worker_error', error_type='FileExistsError')])
            write_json(run / 'c005/result.json', dict(case_id='c005', status='completed', arms={
                arm: dict(trials=[dict(repeat=0, gold=False), dict(repeat=1, gold=False)]) for arm in ARMS}))
            write_json(amendment / 'c005.terminal.json', dict(case_id='c005', status='worker_error',
                error_type='SyntheticStatusWriteFailure'))
            reconcile(run, amendment, [dict(case_id='c005', claimed=True)])
            result = json.loads((run / 'results.json').read_text())[0]
            self.assertEqual(result['status'], 'worker_error')
            self.assertEqual(result['parallel_worker_error_type'], 'SyntheticStatusWriteFailure')
            self.assertEqual(json.loads((run / 'status.json').read_text())['status'], 'completed_with_errors')


if __name__ == '__main__':
    unittest.main()
