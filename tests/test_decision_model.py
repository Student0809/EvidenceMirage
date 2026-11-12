from copy import deepcopy
from dataclasses import replace
import json
import math
import unittest

from src.decision_learning.dataset import complete_pairs, validate_rows
from src.decision_learning.features import FEATURE_NAMES, context_from_history, feature_vector
from src.decision_learning.model import (evaluate_model, fit_model, predict, select_action,
                                         usable_context, validate_model)
from src.engine.history import EvidenceSummary, History, ObservationSummary


def context(trigger='T'):
    return dict(response_signal='Ignored', trigger=trigger,
                predicates=[dict(query='email_received', status=trigger, is_trigger=True)],
                attempts=1, remaining_budget=3, spent_budget=0,
                same_payload_trigger_counts={s: int(s == trigger) for s in ('T', 'F', 'U')},
                observation_error=False)


def row(index=0, *, split='train', trigger='T', retry=True, revise=False):
    return dict(schema_version=1, kind='SYNTHETIC_FIXTURE', pair_id=f'{split}-pair-{index}',
                base_task_id=f'{split}-task-{index}', split=split, status='paired',
                context=context(trigger), outcomes={'Retry': [retry, retry], 'Revise': [revise, revise]},
                payloads={'Retry': 'a' * 64, 'Revise': 'b' * 64},
                protocol=dict(target='fixture-target', attacker='fixture-attacker',
                              observation='fixed-legal-v1', repetitions=2, revision_mode='fixed_candidate'))


def training_rows():
    return [row(i, trigger='T' if i % 2 == 0 else 'F', retry=i % 2 == 0, revise=i % 2 != 0)
            for i in range(12)]


class FeatureBoundaryTests(unittest.TestCase):
    def test_history_ignores_gold_stage_ids_and_hidden_predicates(self):
        evidence = (EvidenceSummary('receipt-id', 'email_received', 'T', True),
                    EvidenceSummary('hidden-id', 'unobservable_private_query', 'U', False))
        episode = ObservationSummary('episode-secret-id', 'payload-hash', 'TRIGGERED_FAIL',
                                     'T', 'Ignored', evidence, False)
        history = History((episode,), 3, 0)
        legal = context_from_history(history)
        changed = replace(episode, episode_id='other-id', payload_sha256='other-hash', stage='SUCCESS',
                          evidence=(replace(evidence[0], predicate_id='other-predicate-id'), evidence[1]))
        self.assertEqual(legal, context_from_history(History((changed,), 3, 0)))
        self.assertEqual(len(legal['predicates']), 1)
        for forbidden in ('gold', 'stage', 'utility', 'payload_sha256', 'episode_id', 'predicate_id'):
            self.assertNotIn(forbidden, json.dumps(legal))
        self.assertEqual(len(feature_vector(legal)), len(FEATURE_NAMES))
        self.assertTrue(all(math.isfinite(v) for v in feature_vector(legal)))

    def test_illegal_context_and_query_rejected(self):
        for extra in ('Gold', 'utility', 'private_state', 'future_outcomes', 'task_id'):
            bad = context()
            bad[extra] = True
            with self.assertRaises(ValueError):
                feature_vector(bad)
        bad = context()
        bad['predicates'][0]['query'] = 'victim_deleted_emails'
        with self.assertRaises(ValueError):
            feature_vector(bad)
        bad = context()
        bad['predicates'][0]['is_trigger'] = False
        with self.assertRaises(ValueError):
            feature_vector(bad)

    def test_unknown_is_preserved_and_runtime_fallback_matches_partial_knowledge(self):
        probabilities = {'Retry': 0.1, 'Revise': 0.9}
        unknown = context('U')
        self.assertFalse(usable_context(unknown))
        self.assertEqual(select_action(probabilities, unknown), 'Retry')
        partial = deepcopy(unknown)
        partial['predicates'].append(dict(query='received_attachment_counts', status='F', is_trigger=True))
        self.assertTrue(usable_context(partial))
        self.assertEqual(select_action(probabilities, partial), 'Revise')
        partial['observation_error'] = True
        self.assertEqual(select_action(probabilities, partial), 'Retry')
        self.assertEqual(unknown['trigger'], 'U')


class PairedDatasetTests(unittest.TestCase):
    def test_group_split_and_duplicate_pair_rejected(self):
        first, other = row(), row(1, split='dev')
        other['base_task_id'] = first['base_task_id']
        with self.assertRaisesRegex(ValueError, 'crosses'):
            validate_rows([first, other])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            validate_rows([first, deepcopy(first)])

    def test_normalized_prompt_group_cannot_cross_splits(self):
        first, other = row(), row(1, split='dev')
        first['group_id'] = other['group_id'] = 'same-normalized-prompt'
        with self.assertRaisesRegex(ValueError, 'group crosses'):
            validate_rows([first, other])
        first['group_id'] = ''
        with self.assertRaisesRegex(ValueError, 'group_id'):
            validate_rows([first])

    def test_unknown_protocol_mix_and_kind_rejected(self):
        first, other = row(), row(1)
        other['protocol']['observation'] = 'privileged-observer'
        with self.assertRaisesRegex(ValueError, 'Mixed protocols'):
            validate_rows([first, other])
        other = row(1)
        other['kind'] = 'LIVE_AGENTDOJO'
        with self.assertRaisesRegex(ValueError, 'cannot be mixed'):
            validate_rows([first, other])

    def test_wrong_labels_and_unequal_arms_rejected(self):
        for invalid in (0, 1, 'false', {}, []):
            bad = row()
            bad['outcomes']['Retry'][0] = invalid
            with self.assertRaisesRegex(ValueError, 'booleans'):
                validate_rows([bad])
        bad = row()
        bad['outcomes']['Retry'].pop()
        with self.assertRaisesRegex(ValueError, 'equal slots'):
            validate_rows([bad])

    def test_errors_missing_slots_and_failed_histories_preserved_in_denominators(self):
        good, missing, short, error = [row(i) for i in range(4)]
        missing['outcomes']['Revise'][0] = None
        short['outcomes'] = {'Retry': [True], 'Revise': [False]}
        error['status'] = 'initial_error'
        for field in ('context', 'outcomes', 'payloads'):
            error.pop(field)
        complete, accounting = complete_pairs([good, missing, short, error], 'train')
        self.assertEqual(complete, [good])
        self.assertEqual(accounting['total_rows'], 4)
        self.assertEqual(accounting['incomplete_pairs'], 2)
        self.assertEqual(accounting['missing_paired_labels'], 3)
        self.assertEqual(accounting['excluded_rows'], 3)
        self.assertIsNone(missing['outcomes']['Revise'][0])
        model = fit_model([good, missing, short, error], steps=5)
        self.assertEqual(model['training_pair_ids'], [good['pair_id']])
        self.assertIn(error['base_task_id'], model['training_base_task_ids'])


class DecisionModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = fit_model(training_rows())

    def test_fit_learns_opposite_actions_and_serializes(self):
        model = self.model
        positive, negative = predict(model, context('T')), predict(model, context('F'))
        self.assertGreater(positive['Retry'], positive['Revise'])
        self.assertLess(negative['Retry'], negative['Revise'])
        self.assertGreater(positive['Retry'], 0.8)
        self.assertGreater(negative['Revise'], 0.8)
        restored = json.loads(json.dumps(model, allow_nan=False))
        self.assertEqual(predict(restored, context()), positive)
        for head in model['training_summary']['heads'].values():
            self.assertLess(head['final_loss'], head['initial_loss'])

    def test_training_does_not_use_holdout_labels_or_order(self):
        heldout = row(33, split='dev')
        fitted = fit_model(training_rows() + [heldout], steps=25)
        heldout['outcomes'] = {'Retry': [False, False], 'Revise': [True, True]}
        refit = fit_model([heldout] + list(reversed(training_rows())), steps=25)
        self.assertEqual(fitted, refit)

    def test_degenerate_fit_reports_warning_and_no_data_does_not_train(self):
        model = fit_model([row(retry=False, revise=False)], steps=10)
        warnings = model['training_summary']['warnings']
        self.assertIn('single_class:Retry', warnings)
        self.assertIn('single_class:Revise', warnings)
        self.assertIn('all_histories_have_equal_arm_outcome_means', warnings)
        with self.assertRaisesRegex(ValueError, 'No complete'):
            fit_model([row(split='dev')])

    def test_corrupt_models_and_nonfinite_options_rejected(self):
        for value in (float('nan'), float('inf'), True):
            invalid = deepcopy(self.model)
            invalid['heads']['Retry']['weights'][0] = value
            with self.assertRaises(ValueError):
                validate_model(invalid)
        invalid = deepcopy(self.model)
        invalid['feature_names'].reverse()
        with self.assertRaises(ValueError):
            validate_model(invalid)
        with self.assertRaises(ValueError):
            fit_model(training_rows(), learning_rate=float('inf'))

    def test_heldout_evaluation_counts_missing_and_compares_same_histories(self):
        heldout = [row(0, split='dev'), row(1, split='dev', trigger='F', retry=False, revise=True)]
        missing = row(2, split='dev')
        missing['outcomes']['Retry'][0] = None
        report = evaluate_model(self.model, heldout + [missing])
        self.assertEqual(report['accounting']['complete_pairs'], 2)
        self.assertEqual(report['accounting']['incomplete_pairs'], 1)
        self.assertEqual(report['selected_mean_outcome'], 1.0)
        self.assertEqual(report['always_retry_mean_outcome'], 0.5)
        self.assertEqual(report['always_revise_mean_outcome'], 0.5)
        self.assertEqual(report['task_macro']['selected'], 1.0)
        self.assertLess(report['per_arm_brier']['Retry'], 0.04)
        self.assertEqual(report['paired_differences']['vs_always_retry']['task_macro_mean'], 0.5)
        json.dumps(report, allow_nan=False)

    def test_heldout_overlap_and_protocol_change_rejected(self):
        heldout = row(0, split='dev')
        heldout['base_task_id'] = self.model['training_base_task_ids'][0]
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            evaluate_model(self.model, [heldout])
        heldout = row(0, split='dev')
        heldout['protocol']['target'] = 'other-target'
        with self.assertRaisesRegex(ValueError, 'protocol'):
            evaluate_model(self.model, [heldout])
        with self.assertRaises(ValueError):
            evaluate_model(self.model, training_rows(), split='train')

    def test_separate_heldout_file_preserves_normalized_prompt_group_exclusion(self):
        train = training_rows()
        train[0]['group_id'] = 'same-public-task-text'
        model = fit_model(train, steps=10)
        self.assertIn('same-public-task-text', model['training_group_ids'])
        self.assertFalse(model['training_summary']['calibrated'])
        heldout = row(23, split='dev')
        heldout['group_id'] = 'same-public-task-text'
        with self.assertRaisesRegex(ValueError, 'normalized-prompt group overlaps'):
            evaluate_model(model, [heldout])
        heldout['group_id'] = 'new-public-task-text'
        report = evaluate_model(model, [heldout])
        self.assertEqual(report['accounting']['complete_groups'], 1)
        self.assertEqual(report['group_macro']['selected'], report['task_macro']['selected'])
        self.assertFalse(report['calibrated'])

    def test_task_macro_gives_each_base_task_equal_weight_and_brier_is_per_execution(self):
        rows = [row(i, split='dev') for i in range(3)]
        rows[1]['base_task_id'] = rows[0]['base_task_id']
        rows[2]['outcomes'] = {'Retry': [False, False], 'Revise': [False, False]}
        report = evaluate_model(self.model, rows)
        self.assertAlmostEqual(report['selected_mean_outcome'], 2 / 3)
        self.assertAlmostEqual(report['task_macro']['selected'], 0.5)
        mixed = row(4, split='dev')
        mixed['outcomes'] = {'Retry': [True, False], 'Revise': [False, True]}
        report = evaluate_model(self.model, [mixed])
        p = predict(self.model, mixed['context'])['Retry']
        self.assertAlmostEqual(report['per_arm_brier']['Retry'], ((p - 1) ** 2 + p ** 2) / 2)

    def test_empty_heldout_and_ties_are_explicit(self):
        report = evaluate_model(self.model, [])
        self.assertIsNone(report['selected_mean_outcome'])
        self.assertEqual(report['accounting']['complete_pairs'], 0)
        self.assertEqual(select_action({'Retry': 0.5, 'Revise': 0.5}, context(), fallback_action='Revise'), 'Revise')
        self.assertEqual(select_action({'Retry': 0.4, 'Revise': 0.5}, context(), revision_penalty=0.2), 'Retry')


if __name__ == '__main__':
    unittest.main()
