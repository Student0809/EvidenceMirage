from copy import deepcopy
import unittest

from src.vendor.plan_references import normalize_plan_references
from src.vendor.revision_planner import validate_plan


class PlanReferenceTests(unittest.TestCase):
    def memory(self):
        return dict(protocol='grounded_history_v1', facts=[
            dict(id='goal', kind='public_input', value='Fixture goal'),
            dict(id='user_task', kind='public_input', value='Fixture task'),
            dict(id='attempt:0:trigger', kind='observed_trigger', value='F'),
            dict(id='attempt:0:unknown', kind='observed_predicate', value='U'),
            dict(id='report', kind='model_report', value='Reported result'),
        ])

    def plan(self):
        return dict(action='make_plan', observations=['user_task'], cause='unknown',
                    hypothesis='A bounded explanation remains uncertain',
                    hypothesis_test=dict(support_refs=['attempt:0:trigger'],
                                         counter_refs=[], unknowns=['Cause unknown'],
                                         predicted_observation='Observe the authorized predicate'),
                    edit_scope='Only clarify the fixture description',
                    preserve=['Fixture constraint'], unknowns=['Cause unknown'],
                    objective='Fixture goal', strategy_rationale='Test the selected fixture strategy',
                    edits=[dict(change='Clarify the description', basis='hypothesis',
                                references=['fact:user_task'], expected_effect='Test the hypothesis')])

    def validate(self, value, memory=None):
        return validate_plan(value, 'Fixture payload', goal='Fixture goal', evidence={},
                             revision_memory=memory or self.memory(),
                             strategy_document='Fixture strategy')

    def test_bare_public_fact_edit_reference_recovers_a_plan(self):
        plan = self.plan()
        plan['edits'][0]['references'] = ['goal', 'user_task']
        self.validate(plan)
        normalized, changes = normalize_plan_references(plan, self.memory())
        self.assertEqual(normalized['edits'][0]['references'], ['goal', 'fact:user_task'])
        self.assertEqual(changes, [dict(path='edits[0].references[1]',
                                       original='user_task', normalized='fact:user_task')])
        self.validate(normalized)

    def test_prefixed_hypothesis_refs_recover_without_changing_claims(self):
        plan = self.plan()
        plan['hypothesis_test']['support_refs'] = ['fact:attempt:0:trigger']
        plan['hypothesis_test']['counter_refs'] = ['fact:user_task']
        normalized, changes = normalize_plan_references(plan, self.memory())
        self.validate(normalized)
        self.assertEqual([x['path'] for x in changes],
                         ['hypothesis_test.support_refs[0]', 'hypothesis_test.counter_refs[0]'])
        for field in ('hypothesis', 'objective', 'edits', 'preserve', 'unknowns'):
            self.assertEqual(normalized[field], plan[field])

    def test_normalization_does_not_authorize_unknown_or_report_observations(self):
        for ref in ('fact:attempt:0:unknown', 'fact:report', 'fact:invented'):
            with self.subTest(ref=ref):
                plan = self.plan()
                plan['observations'] = [ref]
                normalized, _ = normalize_plan_references(plan, self.memory())
                with self.assertRaises(ValueError):
                    self.validate(normalized)

    def test_unknown_edit_and_hypothesis_refs_are_not_membership_checked(self):
        for field in ('edits', 'hypothesis_test'):
            with self.subTest(field=field):
                plan = self.plan()
                if field == 'edits':
                    plan['edits'][0]['references'] = ['invented', 'fact:invented']
                else:
                    plan['hypothesis_test']['support_refs'] = ['fact:invented']
                normalized, changes = normalize_plan_references(plan, self.memory())
                self.assertEqual(normalized, plan)
                self.assertEqual(changes, [])
                self.validate(normalized)

    def test_generic_and_condition_aliases_preserve_their_meaning(self):
        plan = self.plan()
        plan['edits'][0]['references'] = ['goal', 'p0', 'final_report', 'strategy', 'condition:checked']
        memory = self.memory()
        memory['facts'].extend(dict(id=ref, kind='public_input', value='value')
                               for ref in ('p0', 'final_report', 'strategy', 'condition:checked'))
        normalized, changes = normalize_plan_references(plan, memory)
        self.assertEqual(normalized, plan)
        self.assertEqual(changes, [])
        self.validate(normalized, memory)  # References are model annotations, not authorization.

    def test_direct_existing_fact_id_takes_precedence_over_prefix_alias(self):
        plan = self.plan()
        plan['observations'] = ['fact:user_task']
        memory = self.memory()
        memory['facts'].append(dict(id='fact:user_task', kind='model_report', value='A report'))
        normalized, changes = normalize_plan_references(plan, memory)
        self.assertEqual(normalized, plan)
        self.assertEqual(changes, [])
        with self.assertRaises(ValueError):
            self.validate(normalized, memory)

    def test_no_input_mutation_and_normalization_is_idempotent(self):
        plan = self.plan()
        plan['observations'] = ['fact:user_task']
        plan['edits'][0]['references'] = ['user_task']
        memory = self.memory()
        original_plan, original_memory = deepcopy(plan), deepcopy(memory)
        normalized, changes = normalize_plan_references(plan, memory)
        self.assertEqual(plan, original_plan)
        self.assertEqual(memory, original_memory)
        self.assertEqual(len(changes), 2)
        again, again_changes = normalize_plan_references(normalized, memory)
        self.assertEqual(again, normalized)
        self.assertEqual(again_changes, [])

    def test_malformed_fields_and_missing_action_are_not_fabricated(self):
        for value in (None, [], {'observations': 'fact:user_task',
                                 'hypothesis_test': {'support_refs': [None]},
                                 'edits': [None, {'references': 'user_task'}]}):
            with self.subTest(value=value):
                normalized, changes = normalize_plan_references(value, self.memory())
                self.assertEqual(normalized, value)
                self.assertEqual(changes, [])
        plan = self.plan()
        del plan['action']
        normalized, _ = normalize_plan_references(plan, self.memory())
        self.assertNotIn('action', normalized)
        with self.assertRaises(ValueError):
            self.validate(normalized)


if __name__ == '__main__':
    unittest.main()
