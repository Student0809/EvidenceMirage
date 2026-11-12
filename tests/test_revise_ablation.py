"""Offline witnesses for shared-H1 isolation and fixed-slot error accounting."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from scripts.run_revise_ablation_20261030 import (
    ARMS, ROOT, SOURCE, audit_source, build_plan, canonical_hash, counterbalanced_orders,
    execute_case, summarize, verify_plan)
from src.attacker.piminer import GenerationError
from src.engine.config import environment_versions
from src.schema import Episode, Payload, TargetResult


def source_fixture():
    payload = Payload('P0', 'fixed synthetic initial')
    episode = Episode('c000/shared/000', 'c000', 'shared', 'P0', payload.sha256,
        'initial', 'VALID_GOLD_FALSE', False, 'raw final', 'F', 'NOT_TRIGGERED',
        'Ignored', (), None, None, None, None, 0, 0, 'raw/c000/shared/000.json')
    initial = dict(payload=payload.text, payload_sha256=payload.sha256,
        analysis='original analysis', strategy_ids=['one'], strategy_paths=['one.md'],
        route_sha256='fixed-route')
    generation = dict(case_id='c000', policy_id='shared', generation_id='c000/shared/P0',
        status='ready', analysis=initial['analysis'], payload=payload.text,
        payload_sha256=payload.sha256, routing=dict(strategy_ids=['one'], strategy_paths=['one.md']))
    raw = dict(payload=payload.text, result=dict(gold=False, error=None, observation_error=None,
        final='raw final', black_box_final='safe projected final', observation=None, utility=True,
        raw={'secret_state': 'DO_NOT_PROJECT'}), query_packets=[])
    return dict(case_id='c000', goal_spec=dict(predicates=[], trigger_ids=[])), initial, episode.to_dict(), raw, generation


class ReviseAblationTests(unittest.TestCase):
    def test_source_projection_uses_black_box_final_and_original_analysis(self):
        values = source_fixture()
        source = audit_source(*values)
        self.assertEqual(source['attempt']['final_output'], 'safe projected final')
        self.assertEqual(source['attempt']['analysis'], 'original analysis')
        self.assertNotIn('DO_NOT_PROJECT', json.dumps(source))
        self.assertEqual(source['history_sha256'], canonical_hash([source['attempt']]))
        values[3]['result']['black_box_final'] = 'a' * 5000
        self.assertEqual(len(audit_source(*values)['attempt']['final_output']), 4000)

    def test_source_mismatch_error_and_foreign_packets_fail_closed(self):
        for mutate in [
            lambda x: x[3].update(payload='different'),
            lambda x: x[4].update(analysis='different'),
            lambda x: x[2].update(gold=True),
            lambda x: x[3]['result'].update(error='Timeout'),
            lambda x: x[3]['query_packets'].append({'episode_id': 'foreign'}),
            lambda x: x[4]['routing'].update(strategy_ids=['wrong']),
        ]:
            with self.subTest(mutate=mutate):
                values = source_fixture()
                mutate(values)
                with self.assertRaises(ValueError):
                    audit_source(*values)

    def test_counterbalance_deterministic_reverses_target_repeats(self):
        seen = []
        for i in range(4):
            order, repeats = counterbalanced_orders(i, 17)
            self.assertEqual(set(order), set(ARMS))
            self.assertEqual(repeats, [order, order[::-1]])
            self.assertEqual((order, repeats), counterbalanced_orders(i, 17))
            seen.append(order[0])
        self.assertEqual(set(seen), set(ARMS))

    def test_missing_cases_and_generation_failures_keep_all_slots(self):
        result = dict(case_id='a', arms={
            'full_original': dict(trials=[dict(repeat=0, gold=True), dict(repeat=1, gold=False)]),
            'legacy_a2': dict(generation_status='GENERATION_ERROR', trials=[]),
        })
        report = summarize([result], ['a', 'b'])
        baseline = report['arms']['full_original']
        self.assertEqual(baseline['planned_target_slots'], 4)
        self.assertEqual(baseline['confirmed_successes'], 1)
        self.assertEqual(baseline['missing_outcome_bounds'], [0.25, 0.75])
        self.assertIsNone(baseline['complete_case_mean_success_fraction'])
        self.assertEqual(baseline['valid_result_success_fraction'], .5)
        self.assertEqual(report['arms']['legacy_a2']['generation_errors'], 1)
        pair = report['paired_comparisons']['legacy_a2_minus_full_original']
        self.assertEqual(pair['complete_case_pairs'], 0)
        self.assertEqual(pair['missing_case_pairs'], 2)

    def test_case_pairing_uses_means_and_rejects_duplicate_slots(self):
        row = dict(case_id='a', arms={arm: dict(trials=[dict(repeat=0, gold=arm != 'full_original'),
            dict(repeat=1, gold=False)]) for arm in ARMS})
        report = summarize([row], ['a'])
        pair = report['paired_comparisons']['legacy_a2_minus_full_original']
        self.assertEqual(pair['case_wins'], 1)
        self.assertEqual(pair['complete_case_mean_difference'], .5)
        with self.assertRaises(ValueError):
            summarize([row, row], ['a'])
        row['arms']['legacy_a2']['trials'][1]['repeat'] = 0
        with self.assertRaises(ValueError):
            summarize([row], ['a'])

    def test_error_result_cannot_be_promoted_to_a_verdict(self):
        row = dict(case_id='a', arms={'full_original': dict(trials=[
            dict(repeat=0, gold=False, error='SyntheticTimeout', status='TARGET_ERROR')])})
        with self.assertRaisesRegex(ValueError, 'cannot carry a Gold'):
            summarize([row], ['a'])

    def test_fixed_resources_and_environment_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.json'
            path.write_text('original')
            import hashlib
            plan = dict(environment=environment_versions(),
                        inputs_sha256={str(path): hashlib.sha256(path.read_bytes()).hexdigest()})
            verify_plan(plan)
            path.write_text('changed')
            with self.assertRaises(ValueError):
                verify_plan(plan)

    def run_fixture(self, fail_arm=None, error_arm=None):
        case, initial, ep, raw, generation = source_fixture()
        source = audit_source(case, initial, ep, raw, generation)
        log, instances = [], []

        class Attacker:
            def __init__(self, arm):
                self.arm = arm
                self.histories = []
                instances.append(self)

            def bind_run(self, path, policies):
                self.path = path

            def begin_branch(self, current, arm):
                assert arm == self.arm

            def generate_with_history(self, current, payload, episode, packets, revision_id, history):
                if payload is None:
                    return Payload('P0', initial['payload']), deepcopy(generation)
                self.histories.append(deepcopy(history))
                log.append(('generation', self.arm))
                if self.arm == fail_arm:
                    raise GenerationError(dict(status='GENERATION_ERROR', error_type='SyntheticError', calls=[]))
                p1 = Payload('P1', 'synthetic-' + self.arm)
                return p1, dict(status='ready', payload=p1.text, analysis='new',
                    experience={'entries': 0}, calls=[dict(stage='attacker_revise', provider=None)])

        class Target:
            def __init__(self, arm):
                self.arm = arm

            def execute(self, current, payload, episode_id):
                log.append(('target', self.arm))
                if self.arm == error_arm:
                    return TargetResult(None, error='SyntheticTimeout')
                return TargetResult(True, final='synthetic success', utility=True)

        def factory(config):
            arm = next(iter(config['policies']))
            return Target(arm), Attacker(arm)

        plan = dict(kind='SYNTHETIC_TEST_ONLY', environment=environment_versions(), inputs_sha256={},
            cases=[case], case_ids=['c000'], sources={'c000': source}, seed=17,
            arm_configs={arm: dict(observation={}, policies={arm: {}}) for arm in ARMS})
        with tempfile.TemporaryDirectory() as directory:
            result = execute_case(plan, 'c000', directory, factory,
                                  lambda config: SimpleNamespace(collect=lambda *args: ()))
            episode_counts = {arm: len((Path(directory) / 'c000' / arm / 'episodes.jsonl').read_text().splitlines())
                              for arm in ARMS}
        return result, log, instances, episode_counts

    def test_all_candidates_before_scoring_and_success_does_not_stop(self):
        result, log, instances, counts = self.run_fixture()
        self.assertEqual([kind for kind, _ in log], ['generation'] * 4 + ['target'] * 8)
        self.assertEqual(len(instances), 4)
        self.assertEqual({canonical_hash(i.histories[0]) for i in instances}, {result['source_history_sha256']})
        self.assertEqual(set(counts.values()), {2})  # Imported H1 is not charged again.
        self.assertTrue(all(len(a['trials']) == 2 for a in result['arms'].values()))

    def test_generation_and_target_errors_are_missing_without_resampling(self):
        result, log, _, counts = self.run_fixture(fail_arm='legacy_a2', error_arm='a2_v2')
        self.assertEqual(sum(kind == 'generation' for kind, _ in log), 4)
        self.assertEqual(sum(kind == 'target' for kind, _ in log), 6)
        self.assertEqual(counts['legacy_a2'], 0)
        self.assertEqual(counts['a2_v2'], 2)
        summary = summarize([result], ['c000'])
        for arm in ('legacy_a2', 'a2_v2'):
            self.assertEqual(summary['arms'][arm]['missing_slots'], 2)
            self.assertIsNone(summary['arms'][arm]['complete_case_mean_success_fraction'])

    def test_real_resources_and_all_aligned_profiles_offline(self):
        from src.attacker.aligned import PIMinerAlignedAdapter
        from tests.test_piminer_aligned import FakeFactory, FakeTarget
        factory, adapters = FakeFactory(), []

        def make(config):
            options = dict(config['attacker'])
            options.pop('mode')
            attacker = PIMinerAlignedAdapter(ROOT, target_model=config['target']['model'],
                session_factory=factory, **options)
            adapters.append(attacker)
            return FakeTarget(gold=True), attacker

        retrieval = ROOT / 'data/a2/dev.field_v2_20261030.retrieval.json'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'fresh'
            plan = build_plan(SOURCE, retrieval, output)
            self.assertFalse(output.exists())
            result = execute_case(plan, 'c000', output, make,
                lambda config: SimpleNamespace(collect=lambda *args: ()))
            self.assertEqual(len(factory.sessions), 4)
            self.assertEqual(len({s.session_id for s in factory.sessions}), 4)
            for arm in ARMS:
                record = result['arms'][arm]
                self.assertEqual(record['generation_status'], 'ready')
                self.assertEqual(record['generation']['prompt_mode'], 'full')
                self.assertEqual(record['generation']['experience']['entries'], 0)
                self.assertEqual(record['physical_cost']['target_executions'], 2)
                self.assertEqual(record['physical_cost']['generator_model_calls'], 1)
                self.assertEqual(record['generation']['history_iters'], [0])
            self.assertIn('revision_audit', result['arms']['a2_v2_calibrated']['generation'])
            self.assertNotIn('a2', result['arms']['full_original']['generation']['routing'])
            self.assertIn('a2', result['arms']['legacy_a2']['generation']['routing'])
            for adapter in adapters:
                for memory in adapter.memories.values():
                    self.assertEqual(memory.snapshot('c999')[1]['entries'], 0)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                build_plan(SOURCE, retrieval, output)

    def test_v2_arm_rejects_legacy_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'field-embedding V2'):
                build_plan(SOURCE, ROOT / 'data/a2/dev.retrieval.json', Path(directory) / 'fresh')


if __name__ == '__main__':
    unittest.main()
