import copy
from dataclasses import replace
import json
import unittest

from src.evaluation.opportunity_metrics import (
    aggregate_opportunities, case_opportunities, TOKEN_FIELDS,
)
from src.schema import Episode, payload_hash


def episode(index, trigger='F', *, case='case', policy='stage',
            payload='payload-0', gold=False, **changes):
    pid = 'shared' if index == 0 else policy
    row = Episode(
        episode_id=f'{case}/{pid}/{index:03d}', case_id=case, policy_id=pid,
        payload_id='P0', payload_sha256=payload_hash(payload),
        phase='initial' if index == 0 else 'post_failure',
        status='VALID_GOLD_TRUE' if gold is True else 'VALID_GOLD_FALSE',
        gold=gold, final='', trigger=trigger,
        stage='SUCCESS' if gold is True else {'T': 'TRIGGERED_FAIL',
            'F': 'NOT_TRIGGERED', 'U': 'UNOBSERVABLE'}[trigger],
        response_signal='Ignored', evidence=(), observation=None, usage=None,
        error=None, observation_error=None, target_seconds=0.0, query_units=1,
        raw_path=f'raw/{case}/{pid}/{index:03d}.json')
    return replace(row, **changes)


def target_events(rows):
    return [event for e in rows for event in (
        dict(event='TARGET_STARTED', episode_id=e.episode_id,
             payload_sha256=e.payload_sha256),
        dict(event='TARGET_COMPLETED', episode_id=e.episode_id, status=e.status),
    )]


def decision(rows, index, action='Retry', reason='preserve_observed_trigger'):
    current = rows[index - 1]
    return dict(decision_id=f'{current.case_id}/stage/d{index:03d}',
                case_id=current.case_id, policy_id='stage', action=action,
                reason=reason, spent_target_budget=index - 1,
                remaining_target_budget=5 - index,
                visible_history_ids=[e.episode_id for e in rows[:index]])


def calculate(rows=(), decisions=(), events=None, budget=5, complete=False, **kwargs):
    return case_opportunities(rows, decisions,
        target_events(rows) if events is None else events,
        total_target_budget=budget, terminal_complete=complete, **kwargs)


class OpportunityTests(unittest.TestCase):
    def test_por_is_case_level_and_last_exhausted_slot_does_not_qualify(self):
        rows = [episode(i, 'T' if i in (0, 1, 4) else 'F') for i in range(5)]
        result = calculate(rows, complete=True)
        partial = result['partial_opportunity']
        self.assertTrue(partial['case_indicator'])
        self.assertEqual(partial['episode_ids'], [e.episode_id for e in rows[:2]])
        self.assertEqual(partial['budget_exhausted_trigger_true_episode_ids'],
                         [rows[-1].episode_id])
        self.assertEqual([n['remaining_target_budget'] for n in partial['nodes']], [4, 3])
        aggregate = aggregate_opportunities([result])
        self.assertEqual(aggregate['partial_opportunity_rate']['complete_rate'], 1)
        last_only = calculate([episode(i, 'T' if i == 4 else 'F') for i in range(5)],
                              complete=True)
        self.assertFalse(last_only['partial_opportunity']['case_indicator'])

    def test_retry_cap_is_separate_and_follows_payload_bytes_not_label(self):
        rows = [episode(0), episode(1, 'U'), episode(2, 'T', payload_id='P1'),
                episode(3), episode(4)]
        result = calculate(rows, complete=True, max_retries_per_payload=2)
        p = result['partial_opportunity']
        self.assertTrue(p['case_indicator'])
        self.assertFalse(p['retry_cap_actionable_case_indicator'])
        self.assertEqual(p['nodes'][0]['retry_count'], 2)
        zero_cap = calculate([episode(0, 'T')], max_retries_per_payload=0)
        self.assertTrue(zero_cap['partial_opportunity']['case_indicator'])
        self.assertFalse(zero_cap['partial_opportunity']['retry_cap_actionable_observed'])

    def test_unknown_observer_errors_and_success_are_not_partial_failure(self):
        rows = [episode(0, 'U'), episode(1, 'T', observation_error='ObserverError'),
                episode(2, 'T', gold=True)]
        result = calculate(rows, complete=True)
        self.assertFalse(result['partial_opportunity']['case_indicator'])
        coverage = result['evidence_coverage']
        self.assertEqual(coverage['valid_gold_false_episodes'], 2)
        self.assertEqual(coverage['unknown_trigger_failure_episodes'], 2)
        self.assertEqual(coverage['observation_error_failure_episodes'], 1)
        self.assertEqual(coverage['known_trigger_failure_episodes'], 0)

    def test_complete_all_unknown_path_reports_observed_zero_with_coverage(self):
        result = calculate([episode(i, 'U') for i in range(5)], complete=True)
        aggregate = aggregate_opportunities([result])
        self.assertEqual(aggregate['partial_opportunity_rate']['complete_rate'], 0)
        self.assertEqual(aggregate['evidence_coverage']['unknown_evidence_cases'], 1)
        self.assertEqual(aggregate['evidence_coverage']['known_trigger_failure_fraction'], 0)

    def test_errors_and_incomplete_paths_do_not_become_negatives(self):
        error = episode(1, 'T', gold=None, status='TARGET_ERROR', stage='ERROR',
                        error='InternalServerError')
        result = calculate([episode(0), error], complete=True)
        self.assertFalse(result['terminal_complete'])
        self.assertIsNone(result['partial_opportunity']['case_indicator'])
        positive = calculate([episode(0, 'T'), error], complete=True)
        self.assertTrue(positive['partial_opportunity']['case_indicator'])
        aggregate = aggregate_opportunities([positive])
        self.assertEqual(aggregate['partial_opportunity_rate']['observed_lower_bound'], 1)
        self.assertIsNone(aggregate['partial_opportunity_rate']['complete_rate'])
        self.assertEqual(aggregate['partial_opportunity_rate']['incomplete_cases'], 1)
        self.assertIsNone(calculate([])['partial_opportunity']['case_indicator'])

    def test_missing_earlier_episodes_cannot_create_remaining_budget(self):
        result = calculate([episode(4, 'T')], complete=True)
        self.assertFalse(result['partial_opportunity']['observed'])
        self.assertIsNone(result['partial_opportunity']['case_indicator'])
        self.assertFalse(result['terminal_complete'])

    def test_retry_cost_counts_started_and_valid_same_payload_success(self):
        rows = [episode(0), episode(1, 'T', gold=True)]
        decisions = [decision(rows, 1, reason='p0_confirm_trigger_false')]
        result = calculate(rows, decisions, complete=True)
        cost = result['revision_opportunity_cost']
        self.assertEqual((cost['retry_target_slots_started'],
                          cost['retry_target_slots_completed'],
                          cost['retry_target_slots_successes']), (1, 1, 1))
        self.assertEqual(cost['post_initial_target_slots_started'], 1)
        self.assertEqual(cost['retry_share_of_post_initial_slots'], 1)
        self.assertEqual(cost['by_reason_category']['p0_confirmation']['successes'], 1)
        self.assertEqual(cost['retry_direct_attacker_generation_calls'], 0)
        self.assertIsNone(cost['counterfactual_generation_calls'])
        self.assertIsNone(cost['counterfactual_successes'])

    def test_retry_decision_without_start_is_not_consumed_and_unknown_start_is(self):
        rows = [episode(0)]
        decisions = [decision(rows, 1, reason='unknown_budget_schedule')]
        result = calculate(rows, decisions)
        cost = result['revision_opportunity_cost']
        self.assertEqual(cost['retry_target_slots_started'], 0)
        self.assertEqual(cost['by_reason_category']['unknown_fallback']['not_started'], 1)
        events = target_events(rows) + [
            dict(event='TARGET_STARTED', episode_id='case/stage/001',
                 payload_sha256=rows[0].payload_sha256)]
        result = calculate(rows, decisions, events=events)
        cost = result['revision_opportunity_cost']
        self.assertEqual(cost['retry_target_slots_started'], 1)
        self.assertEqual(cost['retry_target_slots_completed'], 0)
        self.assertEqual(cost['by_reason_category']['unknown_fallback']['unresolved'], 1)
        self.assertIsNone(result['partial_opportunity']['case_indicator'])

    def test_all_retry_categories_and_error_cost_without_false_success(self):
        rows = [episode(i) for i in range(5)]
        rows[-1] = replace(rows[-1], gold=None, status='TARGET_ERROR',
                           stage='ERROR', error='TimeoutError')
        reasons = ['p0_confirm_trigger_false', 'unknown_budget_schedule',
                   'preserve_observed_trigger', 'custom_reason']
        result = calculate(rows, [decision(rows, i, reason=reasons[i-1])
                                  for i in range(1, 5)], complete=True)
        cost = result['revision_opportunity_cost']
        self.assertEqual(cost['retry_target_slots_started'], 4)
        self.assertEqual(cost['retry_target_slots_completed'], 4)
        self.assertEqual(cost['retry_target_slots_successes'], 0)
        self.assertEqual(cost['by_reason_category']['other']['errors'], 1)
        self.assertTrue(all(g['started'] == 1 for g in cost['by_reason_category'].values()))

    def test_changed_retry_payload_is_audit_issue_and_not_a_success(self):
        rows = [episode(0), episode(1, payload='changed', gold=True)]
        result = calculate(rows, [decision(rows, 1)], complete=True)
        self.assertEqual(result['revision_opportunity_cost']['retry_target_slots_started'], 1)
        self.assertEqual(result['revision_opportunity_cost']['retry_target_slots_successes'], 0)
        self.assertTrue(result['issues'])
        self.assertFalse(result['terminal_complete'])

    def test_generation_failure_consumes_no_target_slot_but_preserves_known_usage(self):
        rows = [episode(0)]
        gid = 'case/stage/P1'
        usage = dict(zip(TOKEN_FIELDS, (10, 20, 30, 0)))
        records = [dict(generation_id=gid, case_id='case', policy_id='stage',
                        status='GENERATION_ERROR', calls=[
            dict(stage='revision', provider=dict(usage=usage,
                model_usage={'model': {'costUSD': 500, 'inputTokens': 99999}},
                cost={'USD': 10}, cost_basis='unknown'))])]
        events = target_events(rows) + [
            dict(event='GENERATION_STARTED', generation_id=gid),
            dict(event='GENERATION_COMPLETED', generation_id=gid, status='GENERATION_ERROR')]
        result = calculate(rows, [decision(rows, 1, action='Revise')],
                           events=events, generation_records=records)
        self.assertEqual(result['revision_opportunity_cost']['actual_revision_target_started'], 0)
        costs = result['generation_costs']
        self.assertEqual(costs['actual_model_call_attempts'], 1)
        self.assertEqual(costs['tokens_known'], usage)
        self.assertTrue(costs['token_totals_complete'])
        self.assertIsNone(costs['monetary_cost'])
        self.assertIsNone(costs['recorded_amounts_by_currency'])
        self.assertIsNone(costs['physical_provider_requests'])
        self.assertFalse(result['terminal_complete'])

    def test_missing_raw_generation_record_is_missing_not_free(self):
        events = [dict(event='GENERATION_STARTED', generation_id='case/shared/P0'),
                  dict(event='GENERATION_COMPLETED', generation_id='case/shared/P0',
                       status='GENERATION_ERROR')]
        costs = calculate(events=events)['generation_costs']
        self.assertEqual(costs['expected_records'], 1)
        self.assertEqual(costs['available_records'], 0)
        self.assertEqual(costs['missing_records'], 1)
        self.assertEqual(costs['recorded_model_call_attempts'], 0)
        self.assertIsNone(costs['actual_model_call_attempts'])
        self.assertFalse(costs['token_totals_complete'])

    def test_absent_or_malformed_calls_are_unknown_but_explicit_empty_is_zero(self):
        for data in ({}, {'calls': None}, {'calls': {}},
                     {'calls': ['bad-entry']}, {'calls': [None, {'provider': {}}]}):
            with self.subTest(data=data):
                record = dict(generation_id='case/stage/P1', status='ready', **data)
                costs = calculate(generation_records=[record])['generation_costs']
                self.assertIsNone(costs['actual_model_call_attempts'])
                self.assertFalse(costs['model_call_count_complete'])
                self.assertFalse(costs['token_totals_complete'])
                self.assertEqual(costs['recorded_model_call_attempts'],
                                 1 if data == {'calls': [None, {'provider': {}}]} else 0)
        record = dict(generation_id='case/shared/P0', status='ready', calls=[])
        costs = calculate(generation_records=[record])['generation_costs']
        self.assertEqual(costs['actual_model_call_attempts'], 0)
        self.assertTrue(costs['model_call_count_complete'])
        self.assertTrue(costs['token_totals_complete'])
        self.assertIsNone(costs['monetary_cost'])

    def test_missing_usage_keeps_known_lower_bound_without_full_zero_claim(self):
        records = [dict(generation_id='case/stage/P1', status='ready', calls=[
            dict(provider={'usage': {'input_tokens': 7}}), dict(provider={})])]
        result = calculate(generation_records=records)
        costs = result['generation_costs']
        self.assertEqual(costs['actual_model_call_attempts'], 2)
        self.assertEqual(costs['tokens_known']['input_tokens'], 7)
        self.assertEqual(costs['usage_missing_calls'], 1)
        self.assertEqual(costs['token_fields_missing_calls']['output_tokens'], 2)
        self.assertFalse(costs['token_totals_complete'])

    def test_aggregate_preserves_assigned_cases_and_sums_slots_not_fractions(self):
        a = [episode(0, 'T', case='a'), episode(1, 'T', case='a', gold=True)]
        first = calculate(a, [decision(a, 1)], complete=True)
        b = [episode(i, case='b', payload=f'candidate-{i}') for i in range(5)]
        second = calculate(b, [decision(b, i, action='Revise') for i in range(1,5)],
                           complete=True)
        missing = calculate([])
        result = aggregate_opportunities([first, second, missing])
        rate = result['partial_opportunity_rate']
        self.assertEqual(rate['observed_lower_bound'], 1 / 3)
        self.assertIsNone(rate['complete_rate'])
        self.assertEqual(rate['rate_among_complete_cases'], 1 / 2)
        self.assertEqual((rate['confirmed_negative_cases'], rate['unresolved_cases']), (1, 1))
        costs = result['revision_opportunity_cost']
        self.assertEqual(costs['post_initial_target_slots_started'], 5)
        self.assertEqual(costs['retry_share_of_post_initial_slots'], 1 / 5)
        self.assertEqual(costs['actual_revision_target_started'], 4)
        self.assertIsNone(result['generation_costs']['monetary_cost'])

    def test_scope_and_duplicate_events_fail_closed(self):
        rows = [episode(0), episode(1)]
        for bad_rows in ([rows[0], episode(1, case='foreign')],
                         [*rows, episode(2, policy='foreign')], [rows[0], rows[0]]):
            with self.subTest(size=len(bad_rows)):
                with self.assertRaises(ValueError):
                    calculate(bad_rows)
        with self.assertRaises(ValueError):
            calculate(rows, events=target_events(rows) + target_events(rows)[:1])
        result = calculate(rows)
        with self.assertRaises(ValueError):
            aggregate_opportunities([result, result])

    def test_recovered_marker_is_not_double_charged_and_inputs_unchanged(self):
        rows = [episode(0, 'T'), episode(1, gold=True)]
        decisions = [decision(rows, 1)]
        events = target_events(rows) + [
            dict(event='PREFIX_EXECUTION_RECOVERED', episode_id=rows[0].episode_id)]
        before = copy.deepcopy((rows, decisions, events))
        result = calculate(rows, decisions, events=events, complete=True)
        self.assertEqual(result['evidence_coverage']['target_started'], 2)
        self.assertEqual(result['revision_opportunity_cost']['retry_target_slots_started'], 1)
        self.assertEqual((rows, decisions, events), before)
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        self.assertTrue(result['reporting_only'])
        self.assertFalse(result['used_for_decisions'])


if __name__ == '__main__':
    unittest.main()
