from copy import deepcopy
import unittest

from src.attacker.plan_memory import make_plan_memory, project_plan_memory
from src.schema import payload_hash


class PlanMemoryTests(unittest.TestCase):
    def plan(self):
        return dict(hypothesis='A bounded explanation remains a hypothesis.',
                    hypothesis_test=dict(predicted_observation='Observe the same allowed event.',
                                         support_refs=['attempt:0:trigger'],
                                         counter_refs=[], unknowns=['Cause remains unknown.']),
                    preserve=['Keep the public goal constraint.'],
                    unknowns=['Execution mechanism remains unknown.'],
                    edits=[dict(change='Make one bounded edit.', basis='hypothesis',
                                references=['fact:attempt:0:trigger'],
                                expected_effect='Test the visible event.')],
                    strategy_rationale='Test the selected material as a prior.')

    def capsule(self):
        return make_plan_memory(self.plan(), case_id='c013', policy_id='evidence',
                                revision_id='P1', parent_payload_sha256=payload_hash('P0'),
                                payload_sha256=payload_hash('P1'), source_attempt_count=1)

    def project(self, capsule):
        return project_plan_memory(capsule, case_id='c013', policy_id='evidence',
                                   payload_sha256=payload_hash('P1'))

    def test_capsule_keeps_plan_intent_and_explicit_unverified_status(self):
        capsule = self.capsule()
        self.assertEqual(capsule['schema'], 'revision_plan_v1')
        self.assertEqual(capsule['plan_id'], 'c013/evidence/P1')
        self.assertEqual(capsule['status'], 'unverified_model_plan')
        self.assertEqual(capsule['summary']['predicted_observation'],
                         'Observe the same allowed event.')
        self.assertEqual(capsule['summary']['preserve'], self.plan()['preserve'])
        self.assertEqual(capsule['summary']['edits'], self.plan()['edits'])
        self.assertEqual(self.project(capsule), capsule)

    def test_projection_strips_private_metadata_without_mutating_input(self):
        capsule = self.capsule()
        capsule['raw_trajectory'] = 'PRIVATE_TOP_LEVEL'
        capsule['summary']['hidden_state'] = 'PRIVATE_SUMMARY'
        capsule['summary']['edits'][0]['private_judge'] = 'PRIVATE_EDIT'
        original = deepcopy(capsule)
        projected = self.project(capsule)
        self.assertNotIn('PRIVATE_', repr(projected))
        self.assertEqual(capsule, original)
        projected['summary']['preserve'].append('Independent mutation')
        self.assertEqual(capsule, original)

    def test_identity_status_hash_and_source_count_are_validated(self):
        for field, value in [('schema', 'unknown'), ('case_id', 'foreign'),
                             ('policy_id', 'foreign'), ('revision_id', 'P2'),
                             ('plan_id', 'another/id'), ('status', 'verified_fact'),
                             ('payload_sha256', payload_hash('OTHER')),
                             ('parent_payload_sha256', 'bad-hash'),
                             ('source_attempt_count', True), ('source_attempt_count', 0)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.project(dict(self.capsule(), **{field: value}))

    def test_summary_shape_rejects_missing_prediction_and_invalid_edit(self):
        for field, value in [('predicted_observation', ''), ('preserve', 'not a list'),
                             ('unknowns', [None]), ('edits', []),
                             ('strategy_rationale', None)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                capsule = self.capsule()
                capsule['summary'][field] = value
                self.project(capsule)
        capsule = self.capsule()
        capsule['summary']['edits'][0]['basis'] = 'verified_cause'
        with self.assertRaises(ValueError):
            self.project(capsule)

    def review(self):
        return dict(plan_id='c013/evidence/earlier', assessment='inconclusive',
                    decision='adjust', outcome_refs=['attempt:0:trigger'],
                    reason='The visible result does not isolate the cause.',
                    preserve=['Retain the earlier semantic qualifier.'])

    def test_previous_plan_review_survives_as_an_attributed_decision(self):
        plan = self.plan()
        plan['previous_plan_review'] = [self.review()]
        capsule = make_plan_memory(plan, case_id='c013', policy_id='evidence',
            revision_id='P1', parent_payload_sha256=payload_hash('P0'),
            payload_sha256=payload_hash('P1'), source_attempt_count=1)
        self.assertEqual(capsule['summary']['previous_plan_review'], [self.review()])
        self.assertEqual(capsule['status'], 'unverified_model_plan')
        self.assertNotIn('previous_plan_review', self.capsule()['summary'])
        capsule['summary']['previous_plan_review'][0]['private_state'] = 'PRIVATE_REVIEW'
        original = deepcopy(capsule)
        projected = self.project(capsule)
        self.assertEqual(projected['summary']['previous_plan_review'], [self.review()])
        self.assertNotIn('PRIVATE_', repr(projected))
        projected['summary']['previous_plan_review'][0]['preserve'].append('Separate copy')
        self.assertEqual(capsule, original)

    def test_previous_plan_review_rejects_invalid_shape_and_duplicate_identity(self):
        invalid = [None, 'review', [None], [dict(self.review(), assessment='verified')],
                   [dict(self.review(), decision='retry')],
                   [dict(self.review(), plan_id='')],
                   [dict(self.review(), outcome_refs=[])],
                   [dict(self.review(), outcome_refs=[False])],
                   [dict(self.review(), reason='')],
                   [dict(self.review(), preserve=[''])],
                   [self.review(), self.review()]]
        for reviews in invalid:
            with self.subTest(reviews=reviews), self.assertRaises(ValueError):
                capsule = self.capsule()
                capsule['summary']['previous_plan_review'] = reviews
                self.project(capsule)


if __name__ == '__main__':
    unittest.main()
