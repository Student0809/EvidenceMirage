import copy
from dataclasses import replace
import json
import math
import unittest

from src.evaluation.beta_bernoulli import trigger_statistics
from src.schema import Episode, payload_hash


def episode(index, trigger='F', **changes):
    row = Episode(
        episode_id=f'case/stage/{index}', case_id='case', policy_id='stage',
        payload_id='P0', payload_sha256=payload_hash('frozen payload'),
        phase='post_failure', status='VALID_GOLD_FALSE', gold=False, final='',
        trigger=trigger, stage={'T': 'TRIGGERED_FAIL', 'F': 'NOT_TRIGGERED',
                                'U': 'UNOBSERVABLE'}[trigger],
        response_signal='Ignored', evidence=(), observation=None, usage=None,
        error=None, observation_error=None, target_seconds=0.0, query_units=1,
        raw_path=f'raw/{index}.json')
    return replace(row, **changes)


class TriggerStatisticsTests(unittest.TestCase):
    def test_empty_input_is_prior_without_observed_rates(self):
        stats = trigger_statistics(iter(()))
        self.assertEqual(stats['total'], 0)
        self.assertEqual((stats['alpha'], stats['beta']), (1, 1))
        self.assertEqual(stats['mean'], 0.5)
        self.assertAlmostEqual(stats['var'], 1 / 12)
        self.assertAlmostEqual(stats['credible_interval_95']['lower'], 0.025)
        self.assertAlmostEqual(stats['credible_interval_95']['upper'], 0.975)
        self.assertIsNone(stats['observed_trigger_rate'])
        self.assertIsNone(stats['observed_gold_success_rate'])
        self.assertIsNone(stats['coverage']['fraction'])
        self.assertTrue(stats['reporting_only'])
        self.assertFalse(stats['used_for_decisions'])
        self.assertEqual(json.loads(json.dumps(stats, allow_nan=False)), stats)

    def test_true_false_update_posterior_separately_from_gold(self):
        rows = [episode(0, 'T'), episode(1, 'T'),
                episode(2, 'F', gold=True, stage='SUCCESS', status='VALID_GOLD_TRUE')]
        stats = trigger_statistics(iter(rows))
        self.assertEqual((stats['nT'], stats['nF'], stats['nU']), (2, 1, 0))
        self.assertEqual((stats['alpha'], stats['beta']), (3, 2))
        self.assertEqual(stats['mean'], 3 / 5)
        self.assertEqual(stats['var'], 6 / 150)
        self.assertEqual(stats['observed_trigger_rate'], 2 / 3)
        self.assertEqual(stats['observed_gold_success_rate'], 1 / 3)
        self.assertEqual((stats['gold_successes'], stats['gold_failures']), (1, 2))
        self.assertEqual(stats['updated_episode_ids'], [e.episode_id for e in rows])

    def test_errors_and_unknown_do_not_update_or_erase_valid_gold(self):
        rows = [episode(0, 'U'),
                episode(1, 'F', gold=None, error='TimeoutError', stage='ERROR',
                        status='TARGET_ERROR'),
                episode(2, 'T', observation_error='ObserverError', gold=True,
                        stage='SUCCESS', status='VALID_GOLD_TRUE'),
                episode(3, 'U', gold=None, error='TransportError', stage='ERROR',
                        status='TARGET_ERROR', observation_error='ObserverError'),
                episode(4, 'T')]
        stats = trigger_statistics(rows)
        self.assertEqual((stats['nT'], stats['nF'], stats['nU']), (1, 0, 1))
        self.assertEqual((stats['target_errors'], stats['observation_errors'],
                          stats['both_errors']), (2, 2, 1))
        self.assertEqual((stats['alpha'], stats['beta']), (2, 1))
        self.assertEqual((stats['gold_successes'], stats['gold_failures'],
                          stats['gold_missing']), (1, 2, 2))
        self.assertEqual(stats['coverage']['fraction'], 1 / 5)
        self.assertEqual(stats['coverage']['gold_fraction'], 3 / 5)
        self.assertEqual(stats['total'], stats['nT'] + stats['nF'] + stats['nU']
                         + stats['target_errors'] + stats['observation_errors']
                         - stats['both_errors'])
        self.assertEqual(stats['excluded_episode_ids']['unknown_trigger'], [rows[0].episode_id])

    def test_missing_or_error_marked_target_never_becomes_false(self):
        rows = [episode(0, 'F', gold=None),
                episode(1, 'F', status='TARGET_ERROR'),
                episode(2, 'F', stage='ERROR')]
        stats = trigger_statistics(rows)
        self.assertEqual(stats['target_errors'], 3)
        self.assertEqual(stats['gold_failures'], 0)
        self.assertEqual(stats['nF'], 0)
        self.assertEqual((stats['alpha'], stats['beta']), (1, 1))

    def test_identical_episode_ids_are_deduplicated_without_mutation(self):
        first = episode(0, 'T')
        second = episode(1, 'F')
        rows = [first, second, copy.deepcopy(first), first]
        before = copy.deepcopy(rows)
        stats = trigger_statistics(rows)
        self.assertEqual(rows, before)
        self.assertEqual(stats['total'], 2)
        self.assertEqual(stats['duplicate_records_ignored'], 2)
        self.assertEqual(stats['episode_ids'], [first.episode_id, second.episode_id])
        self.assertEqual((stats['alpha'], stats['beta']), (2, 2))

    def test_conflicting_duplicate_id_is_rejected(self):
        first = episode(0)
        for changed in (replace(first, trigger='T'), replace(first, case_id='other'),
                        replace(first, final='different trajectory')):
            with self.subTest(changed=changed.trigger):
                with self.assertRaisesRegex(ValueError, 'Conflicting duplicate'):
                    trigger_statistics([first, changed])

    def test_shared_plus_one_branch_is_allowed_but_cross_groups_are_rejected(self):
        shared = episode(0, policy_id='shared', phase='initial')
        branch = episode(1, 'T')
        stats = trigger_statistics([shared, branch])
        self.assertEqual(stats['policy_ids'], ['shared', 'stage'])
        for changed, message in (
            (replace(branch, case_id='other'), 'cases'),
            (replace(branch, payload_sha256=payload_hash('different')), 'payload'),
            (episode(2, policy_id='other'), 'policy branches'),
        ):
            with self.subTest(message=message):
                rows = [shared, changed] if message != 'policy branches' else [shared, branch, changed]
                with self.assertRaisesRegex(ValueError, message):
                    trigger_statistics(rows)

    def test_same_bytes_with_a_new_payload_label_remain_one_group(self):
        stats = trigger_statistics([episode(0, 'T'), episode(1, 'F', payload_id='P1')])
        self.assertEqual(stats['total'], 2)
        self.assertEqual((stats['alpha'], stats['beta']), (2, 2))

    def test_malformed_records_fail_closed(self):
        with self.assertRaises(TypeError):
            trigger_statistics([episode(0).to_dict()])
        for changed in (replace(episode(0), trigger='unknown'),
                        replace(episode(0), gold=0),
                        replace(episode(0), episode_id=''),
                        replace(episode(0), policy_id='')):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    trigger_statistics([changed])

    def test_equal_tail_intervals_match_closed_form_posteriors(self):
        positive = trigger_statistics([episode(0, 'T')])['credible_interval_95']
        negative = trigger_statistics([episode(0, 'F')])['credible_interval_95']
        self.assertAlmostEqual(positive['lower'], math.sqrt(0.025), places=12)
        self.assertAlmostEqual(positive['upper'], math.sqrt(0.975), places=12)
        self.assertAlmostEqual(negative['lower'], 1 - math.sqrt(0.975), places=12)
        self.assertAlmostEqual(negative['upper'], 1 - math.sqrt(0.025), places=12)
        balanced = trigger_statistics([episode(0, 'T'), episode(1, 'F')])['credible_interval_95']
        for bound, expected_cdf in [('lower', 0.025), ('upper', 0.975)]:
            x = balanced[bound]
            self.assertAlmostEqual(3 * x ** 2 - 2 * x ** 3, expected_cdf, places=12)

    def test_all_five_identical_outcomes_still_have_uncertainty(self):
        positive = trigger_statistics([episode(i, 'T') for i in range(5)])
        negative = trigger_statistics([episode(i, 'F') for i in range(5)])
        self.assertEqual(positive['mean'], 6 / 7)
        self.assertEqual(negative['mean'], 1 / 7)
        self.assertGreater(positive['var'], 0)
        self.assertGreater(positive['credible_interval_95']['lower'], 0)
        self.assertLess(positive['credible_interval_95']['upper'], 1)
        self.assertAlmostEqual(positive['credible_interval_95']['lower'],
                               0.025 ** (1 / 6), places=12)
        self.assertAlmostEqual(positive['credible_interval_95']['lower'],
                               1 - negative['credible_interval_95']['upper'], places=12)


if __name__ == '__main__':
    unittest.main()
