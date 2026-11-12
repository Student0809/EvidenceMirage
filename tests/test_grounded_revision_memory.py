from copy import deepcopy
import json
import unittest

from src.attacker.revision_memory import build_revision_memory
from src.attacker.plan_memory import make_plan_memory
from src.schema import payload_hash


class GroundedRevisionMemoryTests(unittest.TestCase):
    case = dict(case_id='c013', policy_id='evidence',
                injection_task_goal='Fixture public goal', user_task_prompt='Fixture user task')

    def attempt(self, index, payload='P0', states=('T',), *, policy=None,
                trigger=None, observation_error=False, observed=True):
        item = dict(iter=index, injection=payload, analysis='Unverified interpretation',
                    security=False, utility=True, final_output=f'Final report {index}')
        if observed:
            if trigger is None:
                trigger = ('F' if 'F' in states else
                           'T' if states and all(s == 'T' for s in states) else 'U')
            item['revision_observation'] = dict(episode_id=f'c013/{policy or "evidence"}/{index:03}',
                case_id='c013', policy_id=policy or 'evidence', payload_sha256=payload_hash(payload),
                trigger=trigger, observation_error=observation_error,
                predicates=[dict(id=f'p{i}', query='email_received', status=s, is_trigger=True)
                            for i, s in enumerate(states)],
                query_results=[dict(query_id='email_received',
                                    response=dict(supported=True, evidence='Authorized event'))])
        return item

    def test_same_payload_retry_keeps_earlier_full_trigger_witness(self):
        history = [self.attempt(0, states=('T',), policy='shared'),
                   self.attempt(1, states=('F',)), self.attempt(2, 'P1', states=('F',))]
        original = deepcopy(history)
        memory = build_revision_memory(self.case, history)
        self.assertEqual(memory['protocol'], 'grounded_history_v1')
        p0, p1 = memory['candidates']
        self.assertEqual(p0['text'], 'P0')
        self.assertEqual(p0['attempt_iters'], [0, 1])
        self.assertEqual(p0['trigger_counts'], dict(T=1, F=1, U=0))
        self.assertEqual(p0['full_trigger_episode_ids'], ['c013/shared/000'])
        self.assertEqual(memory['positive_reference_candidates'], [payload_hash('P0')])
        self.assertEqual(p1['full_trigger_episode_ids'], [])
        self.assertEqual(memory['base_payload_sha256'], payload_hash('P1'))
        self.assertEqual(history, original)

    def test_predicate_progress_cannot_be_combined_across_episodes(self):
        memory = build_revision_memory(self.case, [self.attempt(0, states=('T', 'F')),
                                                   self.attempt(1, states=('F', 'T'))])
        self.assertEqual(memory['positive_reference_candidates'], [])
        self.assertEqual(memory['candidates'][0]['trigger_counts'], dict(T=0, F=2, U=0))
        predicates = [f for f in memory['facts'] if f['kind'] == 'observed_predicate']
        self.assertEqual([(f['episode_id'], f['predicate_id'], f['value']) for f in predicates],
            [('c013/evidence/000', 'p0', 'T'), ('c013/evidence/000', 'p1', 'F'),
             ('c013/evidence/001', 'p0', 'F'), ('c013/evidence/001', 'p1', 'T')])

    def test_unknown_collection_error_and_legacy_history_have_no_positive_witness(self):
        memory = build_revision_memory(self.case, [self.attempt(0, states=('U',)),
            self.attempt(1, states=('T',), observation_error=True),
            self.attempt(2, observed=False)])
        self.assertEqual(memory['positive_reference_candidates'], [])
        self.assertEqual(memory['candidates'][0]['trigger_counts'], dict(T=0, F=0, U=3))
        self.assertEqual(memory['attempts'][1]['revision_observation']['query_results'], [])
        self.assertEqual(memory['attempts'][1]['revision_observation']['predicates'][0]['status'], 'U')
        self.assertIsNone(memory['attempts'][2]['revision_observation'])
        self.assertFalse(any(f['id'] == 'attempt:2:trigger' for f in memory['facts']))
        self.assertFalse(any('episode_id' in f for f in memory['facts']
                             if f['id'].startswith('attempt:2:')))

    def test_projection_omits_privileged_metadata_and_attributes_model_reports(self):
        item = self.attempt(0)
        for key in ('evidence', 'raw', 'trajectory', 'goal_truth', 'conditions'):
            item[key] = 'PRIVATE_HISTORY_METADATA'
        observation = item['revision_observation']
        observation['raw'] = 'PRIVATE_OBSERVATION_METADATA'
        observation['predicates'][0]['private'] = 'PRIVATE_PREDICATE_METADATA'
        observation['query_results'][0]['raw'] = 'PRIVATE_QUERY_METADATA'
        observation['query_results'][0]['response']['judge_state'] = 'PRIVATE_RESPONSE_METADATA'
        memory = build_revision_memory(self.case, [item])
        rendered = json.dumps(memory)
        self.assertNotIn('PRIVATE_', rendered)
        self.assertEqual(memory['attempts'][0]['analysis_status'], 'unverified_model_analysis')
        self.assertIsNone(memory['attempts'][0]['utility'])
        report = next(f for f in memory['facts'] if f['id'] == 'attempt:0:final_report')
        self.assertEqual(report['kind'], 'model_report')
        self.assertEqual(report['scope'], 'model_report_only')
        self.assertFalse(any(f['kind'] in ('cause', 'analysis') for f in memory['facts']))

    def test_wrong_case_policy_payload_and_duplicate_episode_are_rejected(self):
        for field, value in [('case_id', 'c999'), ('policy_id', 'foreign'),
                             ('payload_sha256', payload_hash('different'))]:
            with self.subTest(field=field):
                item = self.attempt(0)
                item['revision_observation'][field] = value
                with self.assertRaises(ValueError):
                    build_revision_memory(self.case, [item])
        first, second = self.attempt(0), self.attempt(1)
        second['revision_observation']['episode_id'] = first['revision_observation']['episode_id']
        with self.assertRaises(ValueError):
            build_revision_memory(self.case, [first, second])
        unpinned = {k: v for k, v in self.case.items() if k != 'policy_id'}
        with self.assertRaises(ValueError):
            build_revision_memory(unpinned, [self.attempt(0), self.attempt(1, policy='foreign')])

    def test_invalid_failure_or_aggregate_cannot_be_archived_as_positive(self):
        for change in [dict(iter=True), dict(iter=1), dict(security=True),
                       dict(security=None), dict(final_output=None)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                build_revision_memory(self.case, [dict(self.attempt(0), **change)])
        with self.assertRaises(ValueError):
            build_revision_memory(self.case, [self.attempt(0, states=('F',), trigger='T')])

    def test_positive_candidates_keep_first_seen_order_without_ranking(self):
        memory = build_revision_memory(self.case, [self.attempt(0, 'first', states=('F',)),
            self.attempt(1, 'second'), self.attempt(2, 'first'), self.attempt(3, 'second')])
        self.assertEqual([c['text'] for c in memory['candidates']], ['first', 'second'])
        # The reference order is candidate first appearance, not success count.
        self.assertEqual(memory['positive_reference_candidates'],
                         [payload_hash('first'), payload_hash('second')])

    def plan_memory(self, *, revision_id='P1', parent='P0', payload='P1', source_count=1):
        plan = dict(hypothesis='A proposed explanation, not a known cause.',
                    hypothesis_test=dict(predicted_observation='The observed event changes.'),
                    preserve=['Keep the existing semantic qualifier.'],
                    unknowns=['Actual mechanism remains unknown.'],
                    edits=[dict(change='One bounded change.', basis='hypothesis',
                                references=['fact:attempt:0:trigger'],
                                expected_effect='Change the allowed visible event.')],
                    strategy_rationale='A proposed test of the selected strategy.')
        return make_plan_memory(plan, case_id='c013', policy_id='evidence',
                                revision_id=revision_id, parent_payload_sha256=payload_hash(parent),
                                payload_sha256=payload_hash(payload), source_attempt_count=source_count)

    def test_plan_outcomes_pair_one_plan_with_distinct_retry_episodes(self):
        capsule = self.plan_memory()
        history = [self.attempt(0, 'P0', states=('F',)),
                   dict(self.attempt(1, 'P1', states=('F',)), revision_plan=capsule),
                   dict(self.attempt(2, 'P1', states=('T',)), revision_plan=deepcopy(capsule))]
        original = deepcopy(history)
        memory = build_revision_memory(self.case, history)
        self.assertEqual(history, original)
        self.assertEqual(memory['attempts'][1]['revision_plan'], capsule)
        self.assertEqual(len(memory['plan_outcomes']), 1)
        outcome = memory['plan_outcomes'][0]
        self.assertEqual(outcome['plan_id'], capsule['plan_id'])
        self.assertEqual(outcome['revision_plan'], capsule)
        self.assertEqual(outcome['attempt_iters'], [1, 2])
        expected_refs = [f'attempt:{i}:{suffix}' for i in (1, 2)
                         for suffix in ('payload', 'final_report', 'trigger', 'predicate:p0')]
        self.assertEqual(outcome['outcome_refs'], expected_refs)
        facts = {fact['id']: fact for fact in memory['facts']}
        self.assertEqual(facts['attempt:1:trigger']['value'], 'F')
        self.assertEqual(facts['attempt:2:trigger']['value'], 'T')
        self.assertFalse(any(f['kind'] in ('plan', 'model_plan', 'cause') for f in memory['facts']))

    def test_plan_history_rejects_future_source_wrong_base_and_conflicting_identity(self):
        base = self.attempt(0, 'P0', states=('F',))
        for capsule in (self.plan_memory(source_count=2), self.plan_memory(parent='OTHER')):
            with self.subTest(capsule=capsule), self.assertRaises(ValueError):
                build_revision_memory(self.case,
                    [base, dict(self.attempt(1, 'P1'), revision_plan=capsule)])
        with self.assertRaises(ValueError):
            build_revision_memory(self.case,
                [dict(base, revision_plan=self.plan_memory(payload='P0'))])
        conflicting = self.plan_memory()
        conflicting['summary']['hypothesis'] = 'An altered earlier plan.'
        with self.assertRaises(ValueError):
            build_revision_memory(self.case, [base,
                dict(self.attempt(1, 'P1'), revision_plan=self.plan_memory()),
                dict(self.attempt(2, 'P1'), revision_plan=conflicting)])

    def test_legacy_and_unknown_outcomes_remain_unverified_with_plan_memory(self):
        history = [self.attempt(0, 'P0', observed=False),
                   dict(self.attempt(1, 'P1', observed=False), revision_plan=self.plan_memory())]
        memory = build_revision_memory(self.case, history)
        self.assertNotIn('revision_plan', memory['attempts'][0])
        self.assertEqual(memory['plan_outcomes'][0]['outcome_refs'],
                         ['attempt:1:payload', 'attempt:1:final_report'])
        self.assertEqual(memory['attempts'][1]['trigger'], 'U')

    def test_shared_observation_does_not_allow_another_branch_plan(self):
        capsule = self.plan_memory()
        capsule.update(policy_id='foreign', plan_id='c013/foreign/P1')
        with self.assertRaises(ValueError):
            build_revision_memory(self.case, [self.attempt(0, 'P0', policy='shared'),
                dict(self.attempt(1, 'P1', policy='shared'), revision_plan=capsule)])
        # Even without an observed policy, the case pins the permitted branch.
        with self.assertRaises(ValueError):
            build_revision_memory(self.case, [self.attempt(0, 'P0', observed=False),
                dict(self.attempt(1, 'P1', observed=False), revision_plan=capsule)])


if __name__ == '__main__':
    unittest.main()
