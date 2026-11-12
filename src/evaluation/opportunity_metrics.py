"""Case-level observed opportunities and actual Retry slot consumption.

This module is descriptive only: no files, policies, model calls, inferred prices,
or hypothetical generation bills. Inputs are one case and one policy's view,
including its shared initial episode. Engine episode IDs encode post-initial
ordinals, so missing earlier records cannot create fictitious remaining budget.
"""
from collections import Counter
from dataclasses import asdict, is_dataclass
import math


CATEGORIES = ('p0_confirmation', 'unknown_fallback', 'evidence_driven', 'other')
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                'cache_creation_input_tokens')


def _rows(values):
    return [asdict(v) if is_dataclass(v) else dict(v) for v in values]


def _unique(rows, key):
    result = {}
    for row in rows:
        value = row.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError('Missing ' + key)
        if value in result:
            raise ValueError('Duplicate ' + key)
        result[value] = row
    return result


def _identity(identifier):
    parts = identifier.split('/')
    if len(parts) != 3 or not all(parts):
        raise ValueError('Expected case/policy/record identity')
    return parts


def _consumed(identifier):
    _, policy, ordinal = _identity(identifier)
    if not ordinal.isdigit():
        raise ValueError('Invalid Target ordinal')
    number = int(ordinal)
    if (policy == 'shared') != (number == 0):
        raise ValueError('Invalid shared/post-initial Target identity')
    return number + 1


def _valid(episode):
    return (type(episode.get('gold')) is bool and not episode.get('error')
            and episode.get('status') != 'TARGET_ERROR' and episode.get('stage') != 'ERROR')


def _category(reason):
    if reason in ('p0_confirm_trigger_false', 'p0_mixed_trigger_confirmation'):
        return 'p0_confirmation'
    if str(reason).startswith('unknown_'):
        return 'unknown_fallback'
    if reason == 'preserve_observed_trigger':
        return 'evidence_driven'
    return 'other'


def _generation_costs(records, events):
    expected = {e['generation_id'] for e in events
                if e.get('event') in ('GENERATION_STARTED', 'GENERATION_COMPLETED')
                and isinstance(e.get('generation_id'), str)}
    identified = _unique(records, 'generation_id') if records else {}
    missing = expected - set(identified)
    calls, unavailable_call_records, malformed_call_entries = [], 0, 0
    for record in records:
        entries = record.get('calls')
        if not isinstance(entries, list):
            unavailable_call_records += 1
            continue
        for call in entries:
            if isinstance(call, dict):
                calls.append(call)
            else:
                malformed_call_entries += 1
    tokens = dict.fromkeys(TOKEN_FIELDS, 0)
    missing_fields = dict.fromkeys(TOKEN_FIELDS, 0)
    missing_usage = 0
    for call in calls:
        provider = call.get('provider')
        usage = provider.get('usage') if isinstance(provider, dict) else None
        missing_usage += not isinstance(usage, dict)
        usage = usage if isinstance(usage, dict) else {}
        for field in TOKEN_FIELDS:
            value = usage.get(field)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                tokens[field] += value
            else:
                missing_fields[field] += 1
    unknown_events = sum(e.get('event') in ('GENERATION_STARTED', 'GENERATION_COMPLETED')
                         and not e.get('generation_id') for e in events)
    incomplete = bool(missing or unknown_events or unavailable_call_records or malformed_call_entries
                      or any(r.get('status') not in ('ready', 'GENERATION_ERROR') for r in records))
    return dict(
        expected_records=len(expected | set(identified)), available_records=len(records),
        missing_records=len(missing), missing_generation_ids=sorted(missing),
        events_without_generation_id=unknown_events,
        records_with_unavailable_calls=unavailable_call_records,
        malformed_call_entries=malformed_call_entries,
        recorded_model_call_attempts=len(calls),
        actual_model_call_attempts=None if incomplete else len(calls),
        model_call_count_complete=not incomplete,
        calls_by_stage=dict(Counter(c.get('stage', 'unknown') for c in calls)),
        records_by_status=dict(Counter(r.get('status', 'unknown') for r in records)),
        tokens_known=tokens, token_fields_missing_calls=missing_fields,
        usage_missing_calls=missing_usage,
        token_totals_complete=not incomplete and not any(missing_fields.values()),
        monetary_cost=None, recorded_amounts_by_currency=None,
        cost_basis='unknown', cost_missing_calls=len(calls),
        physical_provider_requests=None,
        note=('Calls count recorded outer generator/CLI attempts, not HTTP requests. '
              'Only incremental provider.usage is summed. provider.model_usage and '
              'its costUSD may be cumulative per session; cost basis is unknown and '
              'CLI estimates are not billing. No currency amounts are summed.'))


def case_opportunities(episodes, decisions, events, *, total_target_budget,
                       terminal_complete, generation_records=(),
                       max_retries_per_payload=2):
    """Return observed POR and Retry costs for one case/policy branch.

    POR=True after any valid failed Trigger=T with a remaining execution slot.
    A completed path with no such observation has observed POR=False, including
    all-U paths; this says nothing about unobserved latent opportunities. Errors
    or unfinished paths without a positive observation have indicator=None.

    Retry-cap actionability applies only budget and per-payload retry count; it
    does not predict the policy action or impose policy-specific reservation rules.
    """
    if type(total_target_budget) is not int or total_target_budget < 1:
        raise ValueError('Total budget must include at least the initial execution')
    if type(max_retries_per_payload) is not int or max_retries_per_payload < 0:
        raise ValueError('Invalid retry cap')
    if type(terminal_complete) is not bool:
        raise ValueError('terminal_complete must be boolean')
    episodes, decisions, events, records = map(_rows, (episodes, decisions, events, generation_records))
    by_id = _unique(episodes, 'episode_id')
    _unique(decisions, 'decision_id')
    starts = _unique([e for e in events if e.get('event') == 'TARGET_STARTED'], 'episode_id')
    ends = _unique([e for e in events if e.get('event') == 'TARGET_COMPLETED'], 'episode_id')
    cases, policies = set(), set()
    for row in episodes + decisions + records:
        if row.get('case_id'):
            cases.add(row['case_id'])
        if row.get('policy_id'):
            policies.add(row['policy_id'])
    for identifier in set(by_id) | set(starts) | set(ends):
        cid, pid, _ = _identity(identifier)
        _consumed(identifier)
        cases.add(cid)
        policies.add(pid)
    for event in events + records:
        if event.get('generation_id'):
            cid, pid, _ = _identity(event['generation_id'])
            cases.add(cid)
            policies.add(pid)
    if len(cases) > 1 or len(policies - {'shared'}) > 1:
        raise ValueError('Cannot mix cases or policy branches')
    cid = next(iter(cases), None)
    policy_id = next(iter(policies - {'shared'}), None)
    issues = []
    if set(ends) - set(starts):
        issues.append('completion_without_start')
    if set(by_id) != set(ends):
        issues.append('episode_completion_mismatch')
    if len(starts) > total_target_budget or any(_consumed(e) > total_target_budget for e in starts):
        issues.append('target_budget_exceeded')
    for eid in set(starts) & set(by_id):
        if starts[eid].get('payload_sha256') != by_id[eid].get('payload_sha256'):
            issues.append('started_payload_mismatch:' + eid)
    unresolved = sorted(set(starts) - set(ends))
    trusted = [e for eid, e in by_id.items() if eid in starts and eid in ends
               and starts[eid].get('payload_sha256') == e.get('payload_sha256')]
    trusted.sort(key=lambda e: _consumed(e['episode_id']))
    target_errors = sum(not _valid(e) for e in trusted)
    generation_errors = (any(r.get('status') == 'GENERATION_ERROR' for r in records)
                         or any(e.get('event') == 'GENERATION_COMPLETED'
                                and e.get('status') == 'GENERATION_ERROR' for e in events))
    success = any(_valid(e) and e['gold'] is True for e in trusted)
    full_budget = len(starts) == total_target_budget and len(trusted) == total_target_budget
    complete = bool(terminal_complete and not issues and not unresolved and not target_errors
                    and not generation_errors and (success or full_budget))
    coverage = dict(valid_gold_false_episodes=0, known_trigger_failure_episodes=0,
                    unknown_trigger_failure_episodes=0, observation_error_failure_episodes=0,
                    target_error_episodes=target_errors, target_started=len(starts),
                    target_completed=len(set(starts) & set(ends)),
                    unresolved_target_executions=len(unresolved))
    nodes, exhausted = [], []
    for e in trusted:
        if not _valid(e) or e['gold'] is not False:
            continue
        coverage['valid_gold_false_episodes'] += 1
        observer_error = bool(e.get('observation_error'))
        coverage['observation_error_failure_episodes'] += observer_error
        if observer_error or e.get('trigger') not in ('T', 'F'):
            coverage['unknown_trigger_failure_episodes'] += 1
            continue
        coverage['known_trigger_failure_episodes'] += 1
        if e['trigger'] != 'T':
            continue
        consumed = _consumed(e['episode_id'])
        remaining = max(0, total_target_budget - consumed)
        count = sum(s.get('payload_sha256') == e['payload_sha256'] and _consumed(eid) <= consumed
                    for eid, s in starts.items())
        if remaining == 0:
            exhausted.append(e['episode_id'])
            continue
        nodes.append(dict(episode_id=e['episode_id'], remaining_target_budget=remaining,
                          payload_sha256=e['payload_sha256'], current_payload_attempts=count,
                          retry_count=max(0, count - 1),
                          retry_cap_actionable=count - 1 < max_retries_per_payload))
    observed = bool(nodes)
    actionable_ids = [n['episode_id'] for n in nodes if n['retry_cap_actionable']]
    partial = dict(observed=observed, case_indicator=True if observed else False if complete else None,
                   episode_ids=[n['episode_id'] for n in nodes], nodes=nodes,
                   retry_cap_actionable_observed=bool(actionable_ids),
                   retry_cap_actionable_case_indicator=True if actionable_ids else False if complete else None,
                   retry_cap_actionable_episode_ids=actionable_ids,
                   budget_exhausted_trigger_true_episode_ids=exhausted,
                   actionability_scope='Remaining budget and retry cap only; not the actual policy choice.',
                   interpretation='Observed opportunity only; U and observer errors do not establish absence.')

    categories = {k: dict(decisions=0, started=0, completed=0, successes=0, errors=0,
                          unresolved=0, not_started=0, verified_same_payload_started=0)
                  for k in CATEGORIES}
    transitions, associated = [], set()
    revise_started = revise_completed = revise_successes = 0
    for d in decisions:
        if d.get('action') not in ('Retry', 'Revise'):
            continue
        spent = d.get('spent_target_budget')
        if type(spent) is not int or spent < 0 or not d.get('case_id') or not d.get('policy_id'):
            issues.append('unattributable_decision:' + d['decision_id'])
            continue
        eid = f"{d['case_id']}/{d['policy_id']}/{spent + 1:03d}"
        if eid in associated:
            issues.append('multiple_decisions_for_target:' + eid)
            continue
        associated.add(eid)
        started, ended = eid in starts, eid in starts and eid in ends
        e = by_id.get(eid)
        previous_ids = d.get('visible_history_ids', [])
        previous = by_id.get(previous_ids[-1]) if previous_ids else None
        same_start = bool(started and previous and starts[eid].get('payload_sha256') == previous.get('payload_sha256'))
        same_end = bool(ended and e and previous and e.get('payload_sha256') == previous.get('payload_sha256'))
        gold_success = bool(ended and e and _valid(e) and e['gold'] is True)
        if d['action'] == 'Revise':
            revise_started += started
            revise_completed += ended
            revise_successes += gold_success
            continue
        category = _category(d.get('reason'))
        group = categories[category]
        group['decisions'] += 1
        group['started'] += started
        group['completed'] += ended
        group['successes'] += bool(gold_success and same_start and same_end)
        group['errors'] += bool(ended and e and not _valid(e))
        group['unresolved'] += bool(started and not ended)
        group['not_started'] += not started
        group['verified_same_payload_started'] += same_start
        if started and not same_start:
            issues.append('retry_payload_unverified:' + eid)
        if ended and e and not same_end:
            issues.append('retry_payload_changed:' + eid)
        transitions.append(dict(decision_id=d['decision_id'], episode_id=eid,
                                category=category, started=started, completed=ended,
                                same_payload_on_start=same_start if started else None,
                                same_payload_on_completion=same_end if ended and e else None,
                                success=bool(gold_success and same_start and same_end)))
    # Attribution issues also prevent claiming a fully verified negative case.
    if issues:
        complete = False
        if not observed:
            partial['case_indicator'] = None
        if not actionable_ids:
            partial['retry_cap_actionable_case_indicator'] = None
    retry_started = sum(g['started'] for g in categories.values())
    post_started = sum(_consumed(eid) > 1 for eid in starts)
    cost = dict(
        retry_target_slots_started=retry_started,
        retry_target_slots_completed=sum(g['completed'] for g in categories.values()),
        retry_target_slots_successes=sum(g['successes'] for g in categories.values()),
        post_initial_target_slots_started=post_started,
        retry_share_of_post_initial_slots=retry_started / post_started if post_started else None,
        retry_share_of_total_budget=retry_started / total_target_budget,
        by_reason_category=categories, retry_transitions=transitions,
        actual_revision_target_started=revise_started,
        actual_revision_target_completed=revise_completed,
        actual_revision_target_successes=revise_successes,
        unclassified_post_initial_target_starts=sum(_consumed(eid) > 1 and eid not in associated for eid in starts),
        potential_revision_slots_displaced=retry_started,
        retry_direct_attacker_generation_calls=0,
        counterfactual_generation_calls=None, counterfactual_generation_tokens=None,
        counterfactual_generation_money=None, counterfactual_successes=None,
        interpretation=('Actual started Retry slots occupy executions that could have been allocated '
                        'to revision under an alternative policy. They do not prove lost successes '
                        'or extra generation spending. Retry itself invokes no Attacker generation.'))
    return dict(schema_version=1, case_id=cid, policy_id=policy_id,
                total_target_budget=total_target_budget, terminal_complete=complete,
                terminal_complete_declared=terminal_complete, partial_opportunity=partial,
                evidence_coverage=coverage, revision_opportunity_cost=cost,
                generation_costs=_generation_costs(records, events),
                unresolved_target_ids=unresolved, issues=issues,
                reporting_only=True, used_for_decisions=False)


def aggregate_opportunities(case_results):
    """Aggregate fixed assigned cases; missing cases must be passed as empty rows."""
    rows = [dict(r) for r in case_results]
    identities = [(r.get('case_id'), r.get('policy_id')) for r in rows if r.get('case_id')]
    if len(identities) != len(set(identities)):
        raise ValueError('Duplicate case result')
    policies = {r.get('policy_id') for r in rows if r.get('policy_id')}
    if len(policies) > 1:
        raise ValueError('Aggregate one policy branch at a time')
    n = len(rows)
    complete_rows = [r for r in rows if r['terminal_complete']]

    def rate(observed_key, indicator_key):
        positives = sum(r['partial_opportunity'][observed_key] for r in rows)
        complete_positive = sum(r['partial_opportunity'][observed_key] for r in complete_rows)
        return dict(assigned_cases=n, observed_positive_cases=positives,
                    confirmed_negative_cases=sum(r['partial_opportunity'][indicator_key] is False for r in rows),
                    unresolved_cases=sum(r['partial_opportunity'][indicator_key] is None for r in rows),
                    incomplete_cases=n - len(complete_rows), complete_cases=len(complete_rows),
                    observed_lower_bound=positives / n if n else None,
                    complete_rate=positives / n if n and len(complete_rows) == n else None,
                    rate_among_complete_cases=complete_positive / len(complete_rows) if complete_rows else None)
    categories = {k: {field: sum(r['revision_opportunity_cost']['by_reason_category'][k][field] for r in rows)
                      for field in ('decisions', 'started', 'completed', 'successes', 'errors',
                                    'unresolved', 'not_started', 'verified_same_payload_started')}
                  for k in CATEGORIES}
    fields = ('retry_target_slots_started', 'retry_target_slots_completed', 'retry_target_slots_successes',
              'post_initial_target_slots_started', 'actual_revision_target_started',
              'actual_revision_target_completed', 'actual_revision_target_successes',
              'unclassified_post_initial_target_starts', 'potential_revision_slots_displaced')
    cost = {field: sum(r['revision_opportunity_cost'][field] for r in rows) for field in fields}
    cost.update(by_reason_category=categories,
                retry_share_of_post_initial_slots=(cost['retry_target_slots_started'] / cost['post_initial_target_slots_started']
                                                   if cost['post_initial_target_slots_started'] else None),
                retry_direct_attacker_generation_calls=0, counterfactual_generation_calls=None,
                counterfactual_generation_tokens=None, counterfactual_generation_money=None,
                counterfactual_successes=None)
    gen_fields = ('expected_records', 'available_records', 'missing_records', 'events_without_generation_id',
                  'recorded_model_call_attempts', 'usage_missing_calls', 'cost_missing_calls',
                  'records_with_unavailable_calls', 'malformed_call_entries')
    generation = {field: sum(r['generation_costs'][field] for r in rows) for field in gen_fields}
    generation.update(
        model_call_count_complete=all(r['generation_costs']['model_call_count_complete'] for r in rows),
        tokens_known={f: sum(r['generation_costs']['tokens_known'][f] for r in rows) for f in TOKEN_FIELDS},
        token_fields_missing_calls={f: sum(r['generation_costs']['token_fields_missing_calls'][f] for r in rows) for f in TOKEN_FIELDS},
        token_totals_complete=all(r['generation_costs']['token_totals_complete'] for r in rows),
        monetary_cost=None, recorded_amounts_by_currency=None, cost_basis='unknown',
        physical_provider_requests=None,
        note='Only incremental provider.usage is additive; unknown-basis cumulative model_usage costs are not summed.')
    generation['actual_model_call_attempts'] = (generation['recorded_model_call_attempts']
                                                if generation['model_call_count_complete'] else None)
    coverage_fields = ('valid_gold_false_episodes', 'known_trigger_failure_episodes',
                       'unknown_trigger_failure_episodes', 'observation_error_failure_episodes',
                       'target_error_episodes', 'target_started', 'target_completed', 'unresolved_target_executions')
    coverage = {f: sum(r['evidence_coverage'][f] for r in rows) for f in coverage_fields}
    coverage['unknown_evidence_cases'] = sum(r['evidence_coverage']['unknown_trigger_failure_episodes'] > 0 for r in rows)
    denominator = coverage['valid_gold_false_episodes']
    coverage['known_trigger_failure_fraction'] = coverage['known_trigger_failure_episodes'] / denominator if denominator else None
    return dict(schema_version=1, assigned_cases=n,
                partial_opportunity_rate=rate('observed', 'case_indicator'),
                retry_cap_actionable_opportunity_rate=rate('retry_cap_actionable_observed', 'retry_cap_actionable_case_indicator'),
                evidence_coverage=coverage, revision_opportunity_cost=cost, generation_costs=generation,
                cases_with_issues=sum(bool(r['issues']) for r in rows),
                rate_scope='Observed opportunities, not latent opportunities on unknown/error trajectories.',
                reporting_only=True, used_for_decisions=False)
