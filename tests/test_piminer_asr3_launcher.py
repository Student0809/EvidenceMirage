"""Offline tests of reused-H1 accounting and branch isolation; no model APIs."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.run_piminer_asr3_20261029 import ROOT, ReusedInitialRunner, load_initial
from src.engine.storage import RunStore
from src.evidence.observer import FixedObserver
from src.policies.always_revise import AlwaysRevise
from src.schema import Payload, TargetResult


class DummyAttacker:
    use_cached_p0 = False

    def __init__(self, fail=False):
        self.routes, self.histories, self.fail = {}, [], fail

    def generate_with_history(self, case, payload, last, packets, revision_id, history):
        self.histories.append(deepcopy(history))
        if self.fail:
            raise RuntimeError('Synthetic generation failure')
        result = Payload(revision_id, 'synthetic_' + revision_id)
        return result, dict(status='ready', calls=[], analysis='synthetic analysis', payload=result.text,
                            payload_sha256=result.sha256)


class DummyTarget:
    def __init__(self, success_at=None):
        self.calls, self.success_at = [], success_at

    def execute(self, case, payload, eid):
        self.calls.append(eid)
        return TargetResult(gold=len(self.calls) == self.success_at, utility=False,
                            final='synthetic final', black_box_final='synthetic black-box')


class ReusedInitialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = ROOT / 'runs/observable_pilot5_20261029'
        if not cls.source.exists():
            raise unittest.SkipTest('Local source artifacts unavailable')
        manifest = json.loads((cls.source / 'manifest.json').read_text())
        cls.case = manifest['cases'][0]
        cls.observation = manifest['config']['observation']

    def run_case(self, *, fail=False, success_at=None):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        initial = load_initial(self.source, self.case)
        store = RunStore(Path(self.temp.name) / 'run', {})
        target, attacker = DummyTarget(success_at), DummyAttacker(fail)
        runner = ReusedInitialRunner(target, attacker, FixedObserver(**self.observation),
            {'always_revise': AlwaysRevise()}, store, initial=initial, post_failure_budget=2)
        metrics = runner.run([self.case])
        return runner, target, attacker, metrics

    def test_two_new_executions_and_reused_initial(self):
        runner, target, attacker, metrics = self.run_case()
        self.assertEqual(len(target.calls), 2)
        self.assertEqual(len(runner.episodes), 3)
        self.assertEqual([d['action'] for d in runner.decisions], ['Revise', 'Revise', 'Stop'])
        self.assertEqual(metrics['physical_total_cost']['target_executions'], 2)
        self.assertEqual(metrics['attributed_total_cost']['target_executions'], 3)
        self.assertEqual(metrics['physical_total_cost']['generation_requests'], 2)
        self.assertEqual(metrics['physical_initial_executions'], 0)
        self.assertEqual(metrics['reused_initial_executions'], 1)
        self.assertIs(runner.outcomes[0]['gold_success'], False)
        events = [json.loads(x) for x in (runner.store.path / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(e['event'] == 'TARGET_STARTED' for e in events), 2)
        self.assertEqual(sum(e['event'] == 'INITIAL_EXECUTION_REUSED' for e in events), 1)
        self.assertEqual([len(h) for h in attacker.histories], [1, 2])
        self.assertEqual(attacker.histories[0], [dict(runner.initial['attempt'], iter=0)])
        self.assertEqual(attacker.histories[1][1]['injection'], 'synthetic_P1')
        self.assertEqual(attacker.histories[1][1]['final_output'], 'synthetic black-box')
        self.assertEqual((runner.store.path / runner.episodes[0].raw_path).read_bytes(),
                         runner.initial['raw_path'].read_bytes())

    def test_success_stops_before_third_total_execution(self):
        runner, target, attacker, metrics = self.run_case(success_at=1)
        self.assertEqual(len(target.calls), 1)
        self.assertIs(runner.outcomes[0]['gold_success'], True)
        self.assertEqual([d['action'] for d in runner.decisions], ['Revise', 'Stop'])

    def test_generation_error_is_missing_without_new_execution(self):
        runner, target, attacker, metrics = self.run_case(fail=True)
        self.assertEqual(len(target.calls), 0)
        self.assertIsNone(runner.outcomes[0]['gold_success'])
        self.assertEqual(runner.outcomes[0]['status'], 'GENERATION_ERROR')
        self.assertEqual(metrics['physical_total_cost']['target_executions'], 0)
        self.assertEqual(metrics['attributed_total_cost']['target_executions'], 1)


if __name__ == '__main__':
    unittest.main()
