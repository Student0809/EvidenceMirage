"""Offline, manifest-denominated summary of one supported controller AgentDojo run.

Safe to run while workers are active. No model calls are made. Optional native
verification recomputes saved official scores/evidence only for completed cases.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.engine.config import data_fingerprints
from src.engine.budget import Budget
from src.engine.history import History
from src.policies.registry import build_policies
from src.schema import Episode
from src.evaluation.opportunity_metrics import case_opportunities, aggregate_opportunities

SUPPORTED_POLICIES = frozenset(('always_revise', 'stage_aware', 'stage_aware_p0_confirm'))
P0_CONFIRM_RETRY_REASONS = frozenset(('p0_confirm_trigger_false', 'p0_mixed_trigger_confirmation'))


def selected_policy(config):
    policies = config.get('policies')
    if not isinstance(policies, dict) or len(policies) != 1 or set(policies) - SUPPORTED_POLICIES:
        raise ValueError('expected_single_supported_controller_policy')
    return next(iter(policies))


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def read_rows(path, issues):
    """An in-flight final JSONL append is a snapshot limitation, not a failure."""
    if not path.is_file():
        return []
    text = path.read_text()
    rows = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('record_not_object')
            rows.append(row)
        except (ValueError, TypeError):
            issues.append(dict(code='incomplete_jsonl_tail' if index == len(lines)-1 and not text.endswith('\n')
                               else 'malformed_jsonl_record', file=path.name, line=index+1))
    return rows


def safe_json(path, default, issues):
    try:
        return read_json(path, default)
    except (ValueError, OSError):
        issues.append(dict(code='unreadable_json_snapshot', file=path.name))
        return default


def validate_identity(case_id):
    if not isinstance(case_id, str) or not case_id or Path(case_id).name != case_id or case_id in ('.', '..'):
        raise ValueError('unsafe_case_id')


RECOVERED_ORIGIN = 'recovered_current_run_prefix'


def recovery_context(manifest):
    """Validate the explicit source once; never discover or adopt prior runs."""
    declared = manifest.get('recovery')
    context = dict(enabled=declared is not None, prefix=None, metadata={}, issues=[])
    if declared is None:
        if manifest.get('reused_initial_scope') == 'completed_prefix_of_same_experiment':
            context['issues'].append(dict(code='recovery_manifest_missing'))
        return context
    if not isinstance(declared, dict) or not isinstance(declared.get('source_run'), str):
        context['issues'].append(dict(code='invalid_recovery_manifest'))
        return context
    context['metadata'] = declared
    try:
        from src.engine.prefix_recovery import RecoveryPrefix
        prefix = RecoveryPrefix(declared['source_run'], manifest)
        active = {k: v for k, v in declared.items()
                  if k not in ('changed_input_paths', 'added_input_paths')}
        if prefix.metadata != active:
            raise ValueError('recovery_metadata_does_not_match_validated_source')
        context['prefix'] = prefix
    except Exception as exc:
        context['issues'].append(dict(code='recovery_source_validation_failed', error_type=type(exc).__name__))
    return context


def recovery_case(directory, case, manifest, episodes, events, state, context):
    """Attribute records and require exact source bytes for every carried prefix."""
    issues = list(context['issues'])
    pending = []
    target_events = [event for event in events if event.get('event') in ('TARGET_STARTED', 'TARGET_COMPLETED')]
    markers = [event for event in events if event.get('event') == 'PREFIX_EXECUTION_RECOVERED']
    marked = [event for event in target_events
              if any(key in event for key in ('record_origin', 'source_run', 'source_episode_id'))]
    recovered_ids = {event.get('episode_id') for event in marked + markers}
    started = {event.get('episode_id') for event in target_events if event['event'] == 'TARGET_STARTED'}
    completed = {event.get('episode_id') for event in target_events if event['event'] == 'TARGET_COMPLETED'}
    expected = []
    if not context['enabled'] and (marked or markers):
        issues.append(dict(code='unauthorized_recovered_execution'))
    if context['prefix'] is not None:
        try:
            expected = context['prefix'].for_case(case)
        except Exception as exc:
            issues.append(dict(code='recovery_case_source_invalid', error_type=type(exc).__name__))
    expected_by_id = {item['episode']['episode_id']: item for item in expected}
    by_id = {episode.get('episode_id'): episode for episode in episodes}
    source_run = context['metadata'].get('source_run')
    for event in marked + markers:
        eid = event.get('episode_id')
        if (event.get('record_origin') != RECOVERED_ORIGIN
                or event.get('source_run') != source_run
                or event.get('source_episode_id') != eid
                or eid not in expected_by_id):
            issues.append(dict(code='recovery_event_provenance_mismatch', episode_id=eid))
    prefix_ids = list(expected_by_id)
    present_prefix = [eid for eid in prefix_ids if eid in by_id]
    if [episode.get('episode_id') for episode in episodes[:len(present_prefix)]] != present_prefix:
        issues.append(dict(code='recovery_prefix_sequence_mismatch'))
    if (state == 'completed' or started - set(prefix_ids)) and set(present_prefix) != set(prefix_ids):
        destination = issues if state == 'completed' else pending
        destination.append(dict(code='recovery_prefix_missing_before_continuation'))
    for eid in present_prefix:
        item, episode = expected_by_id[eid], by_id[eid]
        if episode != item['episode']:
            issues.append(dict(code='recovered_episode_mismatch', episode_id=eid))
        for event_type in ('TARGET_STARTED', 'TARGET_COMPLETED', 'PREFIX_EXECUTION_RECOVERED'):
            matches = [event for event in events if event.get('event') == event_type and event.get('episode_id') == eid]
            if len(matches) != 1:
                destination = pending if not matches and state in ('running', 'not_started') else issues
                destination.append(dict(code='recovery_event_missing_or_duplicate', episode_id=eid, event=event_type))
                continue
            event = matches[0]
            if (event.get('record_origin') != RECOVERED_ORIGIN or event.get('source_run') != source_run
                    or event.get('source_episode_id') != item['source_episode_id']):
                issues.append(dict(code='recovery_event_provenance_mismatch', episode_id=eid))
            if event_type == 'PREFIX_EXECUTION_RECOVERED' and event.get('source_raw_sha256') != item['source_raw_sha256']:
                issues.append(dict(code='recovery_raw_provenance_mismatch', episode_id=eid))
        try:
            base = directory.resolve()
            copied = (directory / episode['raw_path']).resolve()
            source_base = (Path(source_run) / case['case_id']).resolve()
            source_raw = (source_base / item['source_raw_path']).resolve()
            if not copied.is_relative_to(base) or not source_raw.is_relative_to(source_base):
                raise ValueError('recovery_raw_path_outside_case')
            source_bytes, copied_bytes = source_raw.read_bytes(), copied.read_bytes()
            if hashlib.sha256(source_bytes).hexdigest() != item['source_raw_sha256']:
                issues.append(dict(code='recovery_source_raw_changed', episode_id=eid))
            if (hashlib.sha256(copied_bytes).hexdigest() != item['source_raw_sha256']
                    or copied_bytes != source_bytes or json.loads(copied_bytes) != item['raw']):
                issues.append(dict(code='recovered_raw_bytes_mismatch', episode_id=eid))
        except (KeyError, OSError, ValueError, TypeError) as exc:
            issues.append(dict(code='recovery_raw_unverifiable', episode_id=eid, error_type=type(exc).__name__))
    # A source episode can appear only with explicit recovery provenance. Merely
    # copying an episode/raw file does not grant it authorization.
    for eid in present_prefix:
        if eid not in recovered_ids:
            destination = pending if state in ('running', 'not_started') else issues
            destination.append(dict(code='recovered_episode_without_provenance', episode_id=eid))
    carried_started = len(started & recovered_ids)
    carried_completed = len(completed & recovered_ids)
    return dict(status='failed' if issues else 'partial' if pending else 'passed' if context['enabled'] else 'not_applicable',
        source_run=source_run, source_prefix_executions=len(expected),
        carried_target_started=carried_started, carried_target_completed=carried_completed,
        new_target_started=len(started - recovered_ids), new_target_completed=len(completed - recovered_ids),
        verified_carried_episodes=len(present_prefix) if not issues and not pending else 0,
        pending_records=pending, issues=issues)


def verify_saved_case(directory, case, manifest, episodes, decisions, events, outcomes, *, recovery=None, recovery_check=None):
    """Replay native score/evidence and policy, without network or model execution."""
    from scripts.verify_observable_pilot5_20261029 import verify_episode, VerificationError
    if recovery is None:
        recovery = recovery_context(manifest)
    if recovery_check is None:
        recovery_check = recovery_case(directory, case, manifest, episodes, events, 'completed', recovery)
    issues, native_results = list(recovery_check['issues']), []
    config = manifest['config']
    budget = config['post_failure_target_budget']
    policy_id = selected_policy(config)
    policy = build_policies(config['policies'])[policy_id]
    cid = case['case_id']
    child = read_json(directory/'manifest.json', {})
    for key in ('config', 'environment'):
        if child.get(key) != manifest.get(key):
            issues.append(dict(code='child_manifest_mismatch', field=key))
    if data_fingerprints(child.get('inputs_sha256', {})) != data_fingerprints(manifest.get('inputs_sha256', {})):
        issues.append(dict(code='child_manifest_mismatch', field='inputs_sha256'))
    if child.get('cases') != [case] or child.get('case_ids') != [cid]:
        issues.append(dict(code='child_case_snapshot_mismatch'))
    expected_ids = ([f'{cid}/shared/000'] + [f'{cid}/{policy_id}/{i:03d}' for i in range(1, len(episodes))]) if episodes else []
    ids = [e.get('episode_id') for e in episodes]
    if ids != expected_ids or len(episodes) > budget+1:
        issues.append(dict(code='episode_sequence_or_budget_mismatch'))
    started = Counter(e.get('episode_id') for e in events if e.get('event') == 'TARGET_STARTED')
    completed = Counter(e.get('episode_id') for e in events if e.get('event') == 'TARGET_COMPLETED')
    if started != completed or completed != Counter(ids):
        issues.append(dict(code='target_event_episode_mismatch'))
    if any('REUSED' in e.get('event', '') for e in events):
        issues.append(dict(code='reused_execution'))
    for episode in episodes:
        try:
            native_results.append(verify_episode(directory, case, episode, config))
        except Exception as exc:
            issues.append(dict(code=str(exc) if isinstance(exc, VerificationError) else type(exc).__name__,
                               episode_id=episode.get('episode_id')))
    replayed = 0
    for index, decision in enumerate(decisions):
        try:
            spent = decision['spent_target_budget']
            if type(spent) is not int or not 0 <= spent < len(episodes):
                raise ValueError('invalid_decision_budget')
            visible = episodes[:spent+1]
            history = History.from_episodes([Episode(**e) for e in visible], Budget(budget, spent))
            if (decision.get('visible_history_ids') != [e['episode_id'] for e in visible]
                    or decision.get('remaining_target_budget') != budget-spent
                    or decision.get('history') != history.summary()
                    or decision.get('current_stage') != visible[-1]['stage']):
                raise ValueError('decision_history_or_budget_mismatch')
            if decision.get('reason') == 'GENERATION_ERROR':
                previous = decisions[index-1] if index else {}
                expected = dict(previous, decision_id=previous.get('decision_id', '')+'/error',
                                action='Stop', reason='GENERATION_ERROR')
                if (decision != expected or previous.get('action') != 'Revise'
                        or not any(e.get('event') == 'GENERATION_COMPLETED' and e.get('status') == 'GENERATION_ERROR'
                                   for e in events)):
                    raise ValueError('generation_error_stop_mismatch')
            else:
                expected = policy.decide(history)
                if (decision.get('action'), decision.get('reason')) != (expected.action.value, expected.reason):
                    raise ValueError('policy_decision_mismatch')
            replayed += 1
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(dict(code=str(exc), decision_id=decision.get('decision_id')))
    if episodes and (not decisions or decisions[-1].get('action') != 'Stop'):
        issues.append(dict(code='completed_case_missing_terminal_stop'))
    if len(outcomes) != 1:
        issues.append(dict(code='missing_or_duplicate_outcome'))
    else:
        outcome = outcomes[0]
        gold = True if any(e.get('gold') is True for e in episodes) else None if outcome.get('status') in (
            'GENERATION_ERROR', 'TARGET_ERROR') else False
        if (outcome.get('gold_success') is not gold
                or outcome.get('target_attempts') != max(0, len(episodes)-1)
                or outcome.get('case_id') != cid or outcome.get('policy_id') != policy_id):
            issues.append(dict(code='outcome_mismatch'))
        if not episodes and outcome.get('status') != 'GENERATION_ERROR':
            issues.append(dict(code='empty_case_without_generation_error'))
    return dict(status='passed' if not issues else 'failed', api_calls=0, issues=issues,
                native_scores_recomputed=sum(r['status'] == 'native_scores_recomputed' for r in native_results),
                target_errors_preserved=sum(r['status'] == 'target_error_preserved' for r in native_results),
                decisions_replayed=replayed)


def analyze_case(run, case, manifest, *, verify_completed=False, recovery=None):
    cid = case['case_id']
    validate_identity(cid)
    directory = run/cid
    issues = []
    config = manifest['config']
    budget = config['post_failure_target_budget']
    policy_id = selected_policy(config)
    cap = (config['policies'][policy_id] or {}).get('max_retries_per_payload', 2)
    state = safe_json(directory/'status.json', {}, issues).get('status', 'not_started')
    episodes, events, decisions = [read_rows(directory/(name+'.jsonl'), issues)
                                  for name in ('episodes', 'events', 'decisions')]
    episodes = [e for e in episodes if e.get('case_id') == cid and e.get('policy_id') in ('shared', policy_id)]
    decisions = [d for d in decisions if d.get('case_id') == cid and d.get('policy_id') == policy_id]
    outcomes = safe_json(directory/'outcomes.json', [], issues)
    outcomes = [o for o in outcomes if o.get('case_id') == cid and o.get('policy_id') == policy_id]
    if len(outcomes) > 1:
        issues.append(dict(code='duplicate_outcome'))
    if recovery is None:
        recovery = recovery_context(manifest)
    recovery_check = recovery_case(directory, case, manifest, episodes, events, state, recovery)
    issues.extend(recovery_check['issues'])
    by_id = {e.get('episode_id'): e for e in episodes}
    if len(by_id) != len(episodes):
        issues.append(dict(code='duplicate_episode'))
    starts = [e for e in events if e.get('event') == 'TARGET_STARTED']
    ends = [e for e in events if e.get('event') == 'TARGET_COMPLETED']
    start_by_id = {e.get('episode_id'): e for e in starts}
    end_by_id = {e.get('episode_id'): e for e in ends}
    if len(starts) != len(start_by_id) or len(ends) != len(end_by_id):
        issues.append(dict(code='duplicate_target_event'))
    if len(starts) > budget+1 or len(episodes) > budget+1:
        issues.append(dict(code='target_budget_exceeded'))
    for eid, event in start_by_id.items():
        if eid in by_id and event.get('payload_sha256') != by_id[eid].get('payload_sha256'):
            issues.append(dict(code='started_payload_mismatch', episode_id=eid))
    next_actions = {}
    for decision in decisions:
        ids = decision.get('visible_history_ids', [])
        if ids and decision.get('reason') != 'GENERATION_ERROR':
            next_actions[ids[-1]] = decision
    timeline, true_nodes = [], []
    for index, episode in enumerate(episodes):
        eid = episode.get('episode_id')
        prior = next((d for d in decisions if d.get('action') in ('Retry', 'Revise')
                      and d.get('spent_target_budget') == index-1), None) if index else None
        decision = next_actions.get(eid, {})
        same_payload_count = sum(e.get('payload_sha256') == episode.get('payload_sha256') for e in episodes[:index+1])
        timeline.append(dict(attempt=index+1, episode_id=eid, action='Initial' if index == 0 else (prior or {}).get('action'),
            action_reason=(prior or {}).get('reason'), gold=episode.get('gold'), trigger=episode.get('trigger'),
            status=episode.get('status'), payload_sha256=episode.get('payload_sha256'),
            record_origin=start_by_id.get(eid, {}).get('record_origin', 'new_execution'),
            next_action=decision.get('action'), next_reason=decision.get('reason')))
        if episode.get('gold') is False and episode.get('trigger') == 'T':
            true_nodes.append(dict(episode_id=eid, attempt=index+1, remaining_target_budget=budget-index,
                remaining_budget_eligible=index < budget, current_payload_attempts=same_payload_count,
                retry_count=same_payload_count-1, retry_cap_eligible=same_payload_count-1 < cap,
                retry_eligible=index < budget and same_payload_count-1 < cap,
                next_action=decision.get('action'), next_reason=decision.get('reason')))
    transitions = []
    for decision in decisions:
        if decision.get('action') not in ('Retry', 'Revise'):
            continue
        spent = decision.get('spent_target_budget')
        if type(spent) is not int or not 0 <= spent < budget:
            issues.append(dict(code='continuation_decision_budget_invalid', decision_id=decision.get('decision_id')))
            continue
        eid = f'{cid}/{policy_id}/{spent+1:03d}'
        history_ids = decision.get('visible_history_ids', [])
        previous = by_id.get(history_ids[-1]) if history_ids else None
        started, ended, episode = start_by_id.get(eid), end_by_id.get(eid), by_id.get(eid)
        start_equal = (started.get('payload_sha256') == previous.get('payload_sha256')) if started and previous else None
        end_equal = (episode.get('payload_sha256') == previous.get('payload_sha256')) if ended and episode and previous else None
        if decision['action'] == 'Retry' and (start_equal is False or end_equal is False):
            issues.append(dict(code='retry_changed_payload', decision_id=decision.get('decision_id')))
        transitions.append(dict(decision_id=decision.get('decision_id'), action=decision['action'], reason=decision.get('reason'),
            episode_id=eid, target_started=started is not None, target_completed=ended is not None and episode is not None,
            same_payload_on_start=start_equal, same_payload_on_completion=end_equal,
            gold=episode.get('gold') if ended and episode else None,
            successful_retry=decision['action'] == 'Retry' and start_equal is True and end_equal is True
                             and episode is not None and episode.get('gold') is True))
    observed_success = any(e.get('gold') is True for e in episodes)
    outcome = outcomes[0] if len(outcomes) == 1 else None
    terminal_error = outcome.get('status') if outcome and outcome.get('status') in ('TARGET_ERROR', 'GENERATION_ERROR') else None
    if observed_success:
        gold_success, disposition = True, 'confirmed_success'
    elif terminal_error:
        gold_success, disposition = None, 'error'
    elif outcome and state == 'completed' and outcome.get('gold_success') is False and len(episodes) == budget+1 and all(
            e.get('gold') is False for e in episodes):
        gold_success, disposition = False, 'complete_failure'
    else:
        gold_success, disposition = None, 'unfinished' if state in ('running', 'not_started') else 'missing_or_invalid'
        if outcome and outcome.get('gold_success') is False:
            issues.append(dict(code='failure_outcome_without_full_budget'))
    row = dict(case_id=cid, policy_id=policy_id, suite_name=case.get('suite_name'), observable_trigger=bool(case.get('goal_spec', {}).get('trigger_ids')),
        worker_status=state, gold_success=gold_success, disposition=disposition, terminal_error=terminal_error,
        target_started=len(start_by_id), target_completed=len(end_by_id), saved_episodes=len(episodes),
        unresolved_target_ids=sorted(set(start_by_id)-set(end_by_id)),
        completed_target_missing_episode_ids=sorted(set(end_by_id)-set(by_id)),
        gold_path=[e.get('gold') for e in episodes], trigger_path=[e.get('trigger') for e in episodes],
        timeline=timeline, gold_false_trigger_true_nodes=true_nodes, transitions=transitions,
        action_counts=dict(Counter(d.get('action') for d in decisions)),
        reason_counts=dict(Counter(d.get('reason') for d in decisions)), recovery=recovery_check, issues=issues)
    generations = []
    seen_generation_paths = set()
    for event in events:
        if event.get('event') != 'GENERATION_COMPLETED' or not event.get('raw_path'):
            continue
        path = (directory / event['raw_path']).resolve()
        if not path.is_relative_to(directory.resolve()):
            issues.append(dict(code='generation_raw_path_outside_case'))
            continue
        if path in seen_generation_paths:
            continue
        seen_generation_paths.add(path)
        record = safe_json(path, None, issues)
        if isinstance(record, dict):
            generations.append(record)
    try:
        row['control_diagnostics'] = case_opportunities(
            episodes, decisions, events, total_target_budget=budget+1,
            terminal_complete=disposition in ('confirmed_success', 'complete_failure'),
            generation_records=generations, max_retries_per_payload=cap)
    except (ValueError, TypeError, KeyError) as exc:
        # Supplementary metrics must not replace an existing ASR snapshot with
        # an exception, nor turn malformed records into zero costs/opportunities.
        row['control_diagnostics'] = dict(status='invalid_records', case_id=cid,
            policy_id=policy_id, error_type=type(exc).__name__, reason=str(exc),
            partial_opportunity=None, revision_opportunity_cost=None, generation_costs=None)
        issues.append(dict(code='invalid_control_diagnostics', error_type=type(exc).__name__))
    # Beta statistics are copied from the selected worker metrics only; no
    # posterior is recomputed, pooled across cases, or fed into policy replay.
    metrics = safe_json(directory/'metrics.json', {}, issues)
    groups = metrics.get('policies', {}) if isinstance(metrics, dict) else None
    policy_metrics = groups.get(policy_id, {}) if isinstance(groups, dict) else None
    if not isinstance(policy_metrics, dict):
        issues.append(dict(code='invalid_worker_metrics'))
        policy_metrics = {}
    posteriors = policy_metrics.get('trigger_posteriors')
    if posteriors is not None:
        if isinstance(posteriors, list):
            row['trigger_posteriors'] = posteriors
        else:
            issues.append(dict(code='invalid_trigger_posteriors_metrics'))
    row['verification'] = dict(status='not_requested')
    if verify_completed:
        if state == 'completed':
            try:
                row['verification'] = verify_saved_case(directory, case, manifest, episodes, decisions, events, outcomes,
                    recovery=recovery, recovery_check=recovery_check)
                row['verification']['issues'].extend(issue for issue in issues
                    if issue not in row['verification']['issues'])
                if row['verification']['issues']:
                    row['verification']['status'] = 'failed'
            except Exception as exc:
                row['verification'] = dict(status='failed', api_calls=0, issues=[dict(code=type(exc).__name__)])
        else:
            row['verification'] = dict(status='deferred_case_unfinished', api_calls=0)
    return row


def coverage_summary(path, selected_ids):
    """Keep the assigned denominator separate from each population exclusion reason."""
    if path is None:
        return dict(status='not_provided', source_population_cases=None, eligible_selected_cases=len(selected_ids),
                    excluded_no_observable_trigger_cases=None, excluded_other_cases=None,
                    selected_ids_match_manifest=None, subset_rerun=None)
    path = Path(path).resolve()
    coverage = read_json(path)
    if not isinstance(coverage, dict):
        raise ValueError('missing_coverage')
    population, selected, excluded = [coverage.get(key) for key in ('population_cases', 'selected_cases', 'excluded_cases')]
    selected_match = coverage.get('selected_case_ids') == selected_ids
    sizes_valid = (all(type(value) is int and value >= 0 for value in (population, selected, excluded))
                   and population == selected+excluded and selected == len(selected_ids))
    exclusions = coverage.get('excluded_case_ids', [])
    population_ids = coverage.get('population_case_ids', [])

    def valid_ids(value):
        return (isinstance(value, list) and all(isinstance(cid, str) and cid for cid in value)
                and len(set(value)) == len(value))

    identities_valid = (sizes_valid and valid_ids(exclusions) and valid_ids(population_ids)
                        and len(exclusions) == excluded and not set(exclusions).intersection(selected_ids)
                        and len(population_ids) == population
                        and set(population_ids) == set(selected_ids) | set(exclusions))
    groups = coverage.get('excluded_case_ids_by_reason',
                          {'no_observable_necessary_trigger': exclusions})
    groups_valid = (isinstance(groups, dict)
                    and not set(groups) - {'no_observable_necessary_trigger', 'already_completed_source_run'}
                    and all(valid_ids(ids) for ids in groups.values()))
    if groups_valid:
        grouped_ids = [cid for ids in groups.values() for cid in ids]
        groups_valid = (valid_ids(exclusions) and len(grouped_ids) == len(set(grouped_ids))
                        and set(grouped_ids) == set(exclusions))
    no_observable = len(groups.get('no_observable_necessary_trigger', [])) if groups_valid else None
    other_excluded = sum(len(ids) for reason, ids in groups.items()
                         if reason != 'no_observable_necessary_trigger') if groups_valid else None
    return dict(status='matched' if selected_match and identities_valid and groups_valid else 'mismatch',
                source=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                source_definition=coverage.get('evaluation_scope'), source_population_cases=population,
                eligible_selected_cases=selected, excluded_no_observable_trigger_cases=no_observable,
                excluded_other_cases=other_excluded, excluded_case_ids_by_reason=groups,
                exclusion_reasons_valid=bool(groups_valid), subset_rerun=coverage.get('rerun'),
                selected_ids_match_manifest=selected_match, counts_consistent=sizes_valid,
                population_partition_valid=bool(identities_valid), excluded_case_ids=exclusions,
                coverage_limitations=coverage.get('coverage_limitations', {}))


def aggregate_control_diagnostics(rows):
    invalid = [r['case_id'] for r in rows
               if r['control_diagnostics'].get('status') == 'invalid_records']
    if invalid:
        return dict(status='invalid_records', assigned_cases=len(rows), invalid_case_ids=invalid,
                    partial_opportunity_rate=None, retry_cap_actionable_opportunity_rate=None,
                    revision_opportunity_cost=None, generation_costs=None, evidence_coverage=None,
                    reason='Malformed diagnostic inputs; aggregate metrics remain unknown. See per-case records.')
    return dict(status='computed', **aggregate_opportunities([r['control_diagnostics'] for r in rows]))


def analyze(run, *, verify_completed=False, coverage=None):
    run = Path(run).resolve()
    manifest = read_json(run/'manifest.json')
    if not isinstance(manifest, dict):
        raise ValueError('missing_manifest')
    cases = manifest.get('cases', [])
    ids = [c['case_id'] for c in cases]
    if not ids or len(set(ids)) != len(ids) or manifest.get('case_ids', ids) != ids:
        raise ValueError('empty_duplicate_or_mismatched_manifest_cases')
    config = manifest['config']
    budget = config['post_failure_target_budget']
    policy_id = selected_policy(config)
    if type(budget) is not int or budget < 0:
        raise ValueError('expected_valid_post_failure_target_budget')
    selection = coverage_summary(coverage, ids)
    recovery = recovery_context(manifest)
    rows = [analyze_case(run, case, manifest, verify_completed=verify_completed, recovery=recovery) for case in cases]
    n = len(rows)
    successes = sum(r['gold_success'] is True for r in rows)
    failures = sum(r['gold_success'] is False for r in rows)
    missing = n-successes-failures
    transitions = [t for r in rows for t in r['transitions']]
    retries = [t for t in transitions if t['action'] == 'Retry']
    revisions = [t for t in transitions if t['action'] == 'Revise']
    failed = [e for r in rows for e in r['timeline'] if e['gold'] is False]
    nodes = [t for r in rows for t in r['gold_false_trigger_true_nodes']]
    actions, reasons = Counter(), Counter()
    for row in rows:
        actions.update(row['action_counts'])
        reasons.update(row['reason_counts'])
    verification = dict(requested=verify_completed, api_calls=0,
        passed_cases=sum(r['verification']['status'] == 'passed' for r in rows),
        failed_cases=sum(r['verification']['status'] == 'failed' for r in rows),
        deferred_cases=sum(r['verification']['status'] == 'deferred_case_unfinished' for r in rows),
        native_scores_recomputed=sum(r['verification'].get('native_scores_recomputed', 0) for r in rows),
        decisions_replayed=sum(r['verification'].get('decisions_replayed', 0) for r in rows))
    verification['status'] = ('not_requested' if not verify_completed else 'failed' if verification['failed_cases'] else
                              'partial' if verification['deferred_cases'] else 'passed')
    warnings = [dict(case_id=r['case_id'], **issue) for r in rows for issue in r['issues']]
    run_state = read_json(run/'status.json', {}).get('status', 'unknown')
    if selection['status'] == 'mismatch':
        warnings.append(dict(code='coverage_manifest_mismatch'))
    recovery_summary = dict(enabled=recovery['enabled'],
        status='failed' if recovery['issues'] or any(row['recovery']['status'] == 'failed' for row in rows)
               else 'partial' if any(row['recovery']['status'] == 'partial' for row in rows)
               else 'passed' if recovery['enabled'] else 'not_applicable',
        source_run=recovery['metadata'].get('source_run'),
        source_manifest_sha256=recovery['metadata'].get('source_manifest_sha256'),
        source_prefix_target_executions=recovery['metadata'].get('prefix_target_executions', 0),
        source_validation='passed' if recovery['prefix'] is not None else 'failed' if recovery['enabled'] else 'not_applicable',
        source_preflight=recovery['metadata'].get('source_preflight'),
        carried_target_started=sum(row['recovery']['carried_target_started'] for row in rows),
        carried_target_completed=sum(row['recovery']['carried_target_completed'] for row in rows),
        new_target_started=sum(row['recovery']['new_target_started'] for row in rows),
        new_target_completed=sum(row['recovery']['new_target_completed'] for row in rows),
        verified_carried_episodes=sum(row['recovery']['verified_carried_episodes'] for row in rows),
        pending_record_count=sum(len(row['recovery']['pending_records']) for row in rows),
        issues=recovery['issues'],
        accounting='Recorded total counts each carried prefix and new execution once; source counts are not added again.')
    # During startup some validated source prefixes may not yet be copied into a
    # worker directory. Keep that known physical work visible separately from
    # the destination log counts, without adding the source twice.
    valid_source_count = (recovery_summary['source_prefix_target_executions']
                          if recovery_summary['source_validation'] == 'passed' else 0)
    recovery_summary['total_unique_target_started'] = valid_source_count + recovery_summary['new_target_started']
    recovery_summary['total_unique_target_completed'] = valid_source_count + recovery_summary['new_target_completed']
    return dict(source=str(run), generated_at=datetime.now(timezone.utc).isoformat(),
        manifest_sha256=hashlib.sha256((run/'manifest.json').read_bytes()).hexdigest(),
        status='complete' if missing == 0 else 'incomplete', run_status=run_state,
        policy_id=policy_id, assigned_cases=n, completed_workers=sum(r['worker_status'] == 'completed' for r in rows),
        total_target_budget=budget+1, post_failure_target_budget=budget, coverage=selection,
        design=dict(policy_id=policy_id, split=config.get('split'), subset_rerun=selection.get('subset_rerun'),
                    independent_heldout=manifest.get('independent_heldout', False),
                    independent_heldout_established=False, single_arm=True, causal_effect_estimated=False,
                    attacker_mode=config.get('attacker', {}).get('mode'),
                    observable_trigger_cases=sum(r['observable_trigger'] for r in rows),
                    no_observable_trigger_cases=sum(not r['observable_trigger'] for r in rows)),
        asr=dict(confirmed_successes=successes, complete_failures=failures, unknown_or_error_outcomes=missing,
                 confirmed_success_fraction=successes/n, complete_asr=successes/n if missing == 0 else None),
        target_started=sum(r['target_started'] for r in rows), target_completed=sum(r['target_completed'] for r in rows),
        saved_episodes=sum(r['saved_episodes'] for r in rows),
        action_counts=dict(actions), reason_counts=dict(reasons),
        retry=dict(decisions=len(retries), evidence_driven_decisions=sum(t['reason'] == 'preserve_observed_trigger' for t in retries),
                   unknown_fallback_decisions=sum(str(t['reason']).startswith('unknown_') for t in retries),
                   p0_confirmation_decisions=sum(t['reason'] in P0_CONFIRM_RETRY_REASONS for t in retries),
                   other_reason_decisions=sum(t['reason'] != 'preserve_observed_trigger'
                       and not str(t['reason']).startswith('unknown_')
                       and t['reason'] not in P0_CONFIRM_RETRY_REASONS for t in retries),
                   target_started=sum(t['target_started'] for t in retries),
                   target_completed=sum(t['target_completed'] for t in retries),
                   unchanged_payload_starts=sum(t['target_started'] and t['same_payload_on_start'] is True for t in retries),
                   unchanged_payload_completions=sum(t['target_completed'] and t['same_payload_on_start'] is True
                                                     and t['same_payload_on_completion'] is True for t in retries),
                   successful_executions=sum(t['successful_retry'] for t in retries),
                   success_cases=sum(any(t['successful_retry'] for t in r['transitions']) for r in rows)),
        revise=dict(decisions=len(revisions), target_started=sum(t['target_started'] for t in revisions),
                    target_completed=sum(t['target_completed'] for t in revisions)),
        trigger_diagnostic=dict(valid_failed_executions=len(failed), gold_false_trigger_true_executions=len(nodes),
            frequency_among_valid_failures=len(nodes)/len(failed) if failed else None,
            remaining_budget_eligible=sum(t['remaining_budget_eligible'] for t in nodes),
            retry_cap_eligible=sum(t['retry_cap_eligible'] for t in nodes),
            both_budget_and_retry_cap_eligible=sum(t['retry_eligible'] for t in nodes),
            eligible_observed_retry_decisions=sum(t['retry_eligible'] and t['next_action'] == 'Retry'
                                                 and t['next_reason'] == 'preserve_observed_trigger' for t in nodes)),
        trigger_posteriors=dict(statistics_only=True, used_for_decisions=False,
            source='worker_metrics.policies[policy_id].trigger_posteriors',
            recorded_cases=sum('trigger_posteriors' in row for row in rows),
            by_case={row['case_id']: row['trigger_posteriors'] for row in rows if 'trigger_posteriors' in row}),
        control_diagnostics=dict(
            **aggregate_control_diagnostics(rows),
            first_decision_disagreement=dict(status='not_available_single_arm',
                mode='recorded_pairwise',
                reason='Requires two recorded arms with a verified common execution prefix; use summarize_control_diagnostics.py.')),
        verification=verification, recovery=recovery_summary, issues=warnings, cases=rows,
        limitations=['A full benchmark run is not automatically an independent held-out evaluation.',
                     'Single-arm outcomes do not estimate a causal Evidence or Retry advantage.',
                     'A running snapshot can contain events whose following records have not been flushed yet.',
                     'Native verification replays saved legal views; it does not independently recapture them.'])


def render_control_metrics(diagnostics):
    """Compact human-readable view; missing and counterfactual costs stay unknown."""
    if not diagnostics:
        return []
    if diagnostics.get('status') == 'invalid_records':
        return ['三个控制指标暂不可计算：记录异常，结果保持未知。案例：' + ', '.join(diagnostics['invalid_case_ids']) + '。']
    opportunity = diagnostics['partial_opportunity_rate']
    cost = diagnostics['revision_opportunity_cost']
    generation = diagnostics['generation_costs']
    coverage = diagnostics['evidence_coverage']
    rate = opportunity['complete_rate']
    rate_text = '未完整，不能作为完整比例' if rate is None else f'{rate:.2%}'
    share = cost['retry_share_of_post_initial_slots']
    share_text = '不可计算' if share is None else f'{share:.2%}'
    lines = [
        f"Partial Opportunity Rate：{opportunity['observed_positive_cases']}/{opportunity['assigned_cases']}"
        f"（{rate_text}）；按案例计数，只纳入有效 Gold=False、Trigger=T 且执行后仍有 Target 预算的记录。", '',
        f"Revision Opportunity Cost：Retry 已占用 {cost['retry_target_slots_started']} 个 Target 槽位，"
        f"占首轮之后 {cost['post_initial_target_slots_started']} 个已开始槽位的 {share_text}；"
        f"完成 {cost['retry_target_slots_completed']} 个，其中成功 {cost['retry_target_slots_successes']} 个。", '',
        '| Retry 原因 | 决策次数 | 已占用槽位 | 已完成 | 成功 |',
        '|---|---:|---:|---:|---:|',
    ]
    labels = {'p0_confirmation': 'P0 确认', 'unknown_fallback': 'U 回退',
              'evidence_driven': '正向 Evidence', 'other': '其他'}
    for key, label in labels.items():
        row = cost['by_reason_category'][key]
        lines.append(f"| {label} | {row['decisions']} | {row['started']} | {row['completed']} | {row['successes']} |")
    tokens = generation['tokens_known']
    lines += ['',
        f"实际记录的生成调用（外层调用/CLI 轮次，不等同 HTTP 请求）：{generation['recorded_model_call_attempts']} 次。"
        f"缺失生成记录 {generation['missing_records']} 份，缺失 usage 的已记录调用 {generation['usage_missing_calls']} 次。", '',
        f"已知增量 token：输入 {tokens['input_tokens']}、输出 {tokens['output_tokens']}、"
        f"缓存读取 {tokens['cache_read_input_tokens']}、缓存写入 {tokens['cache_creation_input_tokens']}；"
        f"token 统计完整：{generation['token_totals_complete']}。", '',
        'Retry 本身不生成新候选；被替代修订的生成 token、金额和成功收益未知。货币成本未计价，不按零处理。', '',
        f"存在未知 Trigger 的案例：{coverage['unknown_evidence_cases']}；"
        '完整 U 轨迹的已观察机会可为 0，这不表示已排除未能观测的机会。', '',
    ]
    disagreement = diagnostics.get('first_decision_disagreement')
    if disagreement:
        lines.append('First Decision Disagreement：' + disagreement['status'] + '。')
    return lines


def render_markdown(report):
    a, t, r = report['asr'], report['trigger_diagnostic'], report['retry']
    c = report['coverage']
    subset_rerun = report.get('design', {}).get('subset_rerun')
    title = '# AgentDojo Evidence 错误子集重跑记录' if isinstance(subset_rerun, dict) else '# AgentDojo Evidence 全量运行记录'
    lines = [title, '',
        f"- 策略：{report.get('policy_id', report.get('design', {}).get('policy_id', 'stage_aware'))}。",
        f"- 运行状态：{report['run_status']}；结果完整性：{report['status']}。",
        f"- 固定分母：{report['assigned_cases']}；已结束 worker：{report['completed_workers']}。",
        f"- 原始测试人口：{c['source_population_cases']}；满足观察条件并入选：{c['eligible_selected_cases']}；"
        f"无可观察 Trigger 而排除：{c['excluded_no_observable_trigger_cases']}；其他原因未纳入本轮：{c.get('excluded_other_cases', 0)}"
        f"（均不计入本轮缺失）。覆盖对齐：{c['status']}。",
        f"- Total ASR@{report['total_target_budget']}：首测 1 次 + 后续最多 {report['post_failure_target_budget']} 次。",
        f"- 确认成功 {a['confirmed_successes']}，完整失败 {a['complete_failures']}，未完成或错误 {a['unknown_or_error_outcomes']}。",
        f"- 完整 ASR：{a['complete_asr']}；确认成功/固定分母：{a['confirmed_success_fraction']:.6f}（缺失存在时不是完整 ASR）。",
        f"- Target 已开始 {report['target_started']} 次，已完成 {report['target_completed']} 次。", '',
        f"Gold=False、Trigger=T：{t['gold_false_trigger_true_executions']} / {t['valid_failed_executions']} 次有效失败；"
        f"其中剩余预算允许 {t['remaining_budget_eligible']}，重试次数上限允许 {t['retry_cap_eligible']}，两者均允许 {t['both_budget_and_retry_cap_eligible']}。", '',
        f"Retry 决策 {r['decisions']} 次：Evidence 驱动 {r['evidence_driven_decisions']}，U 回退 {r['unknown_fallback_decisions']}，"
        f"P0 确认 {r.get('p0_confirmation_decisions', 0)}，其他原因 {r['other_reason_decisions']}。"
        f"实际开始 {r['target_started']} 次、完成 {r['target_completed']} 次；开始与完成均确认 Payload 不变 {r['unchanged_payload_completions']} 次，"
        f"成功 {r['successful_executions']} 次。Revise 决策 {report['revise']['decisions']} 次。", '',
        f"离线核验：{report['verification']['status']}；已复核原生评分 {report['verification']['native_scores_recomputed']} 次；"
        f"日志问题 {len(report['issues'])} 项。", '',
        '单组运行不能证明 Evidence 或 Retry 的因果收益；benchmark 也不等于独立 held-out 测试集。', '',
        '| 样本 | 状态 | Target 完成 | Gold 路径 | Trigger 路径 | Retry 决策 |',
        '|---|---|---:|---|---|---:|']
    diagnostic_lines = render_control_metrics(report.get('control_diagnostics'))
    if diagnostic_lines:
        position = next(i for i, line in enumerate(lines) if line.startswith('| 样本 |'))
        lines[position:position] = diagnostic_lines + ['']
    if isinstance(subset_rerun, dict):
        restart = ('从固定历史 P0 重新执行' if subset_rerun.get('restart') == 'fresh_from_fixed_p0'
                   else '重启方式：' + str(subset_rerun.get('restart')))
        retained = ('此前有效结果单独保留。' if subset_rerun.get('previous_valid_outcomes_retained_separately') is True else '')
        position = next(i for i, line in enumerate(lines) if line.startswith('| 样本 |'))
        lines[position:position] = [
            f"错误子集重跑：来源 {subset_rerun.get('source_run')}，选择错误类型 {subset_rerun.get('selected_error')}；{restart}。",
            f"本次 {report['assigned_cases']} 例独立报告不等价于完整 {subset_rerun.get('source_selected_cases')} 例 ASR；{retained}", '']
    beta = report.get('trigger_posteriors', {})
    if beta.get('recorded_cases'):
        position = next(i for i, line in enumerate(lines) if line.startswith('| 样本 |'))
        lines[position:position] = [
            f"Beta Trigger 后验：{beta['recorded_cases']} 个案例有记录，按案例原样保存在 JSON 中；仅用于统计，不参与策略决策。", '']
    recovery = report.get('recovery', {})
    if recovery.get('enabled') or recovery.get('status') == 'failed':
        recovery_lines = [
            f"显式前缀恢复：来源 {recovery.get('source_run')}；来源校验 {recovery.get('source_validation')}；"
            f"复制校验 {recovery.get('status')}。",
            f"来源已完成 {recovery.get('source_prefix_target_executions')} 次；本目录已记录恢复前缀 "
            f"{recovery.get('carried_target_completed')} 次，新实际执行开始/完成 "
            f"{recovery.get('new_target_started')}/{recovery.get('new_target_completed')} 次。"
            f"来源与新增去重后累计完成 {recovery.get('total_unique_target_completed')} 次；"
            "已恢复前缀不重发请求、不重新增加预算，来源预检也不重复计费。", '']
        # Insert before the per-case table without changing ordinary-run output.
        position = next(i for i, line in enumerate(lines) if line.startswith('| 样本 |'))
        lines[position:position] = recovery_lines
    for case in report['cases']:
        lines.append(f"| {case['case_id']} | {case['disposition']} | {case['target_completed']} | "
                     + ','.join('T' if g is True else 'F' if g is False else 'null' for g in case['gold_path'])
                     + ' | '+','.join(str(t) for t in case['trigger_path'])
                     + f" | {case['action_counts'].get('Retry', 0)} |")
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--output-prefix', required=True, type=Path)
    parser.add_argument('--verify-completed', action='store_true')
    parser.add_argument('--coverage', type=Path, help='Original population/eligible/excluded selection inventory.')
    args = parser.parse_args(argv)
    report = analyze(args.run, verify_completed=args.verify_completed, coverage=args.coverage)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    args.output_prefix.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    args.output_prefix.with_suffix('.md').write_text(render_markdown(report))
    print(json.dumps({k: report[k] for k in ('status', 'run_status', 'assigned_cases', 'completed_workers', 'asr', 'retry', 'verification')}))
    return report


if __name__ == '__main__':
    main()
