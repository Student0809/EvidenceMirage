"""Read-only mixed F/U cost snapshots from durable records; no API imports.

Only whitelisted identifiers, statuses, usage and timings enter the output.
Payloads, prompts, plans, final responses and private trajectories are omitted.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics


ROOT = Path(__file__).resolve().parents[1]


def read_json(path, issues, default=None):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        issues.append(dict(code='unreadable_json_snapshot', file=path.name))
        return default


def read_rows(path, issues):
    if not path.is_file():
        return []
    text = path.read_text()
    rows, lines = [], text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('not_an_object')
            rows.append(row)
        except (ValueError, TypeError):
            tail = index == len(lines)-1 and not text.endswith('\n')
            issues.append(dict(code='incomplete_jsonl_tail' if tail else 'malformed_jsonl_record',
                               file=path.name, line=index+1))
    return rows


def number(value):
    return value if type(value) in (int, float) and value >= 0 else None


def usage_view(usage):
    """Prefer per-call usage; model_usage in resumed sessions can be cumulative."""
    if not isinstance(usage, dict):
        return dict(available=False, input_tokens=None, output_tokens=None,
                    cache_read_input_tokens=None, cache_creation_input_tokens=None)
    def field(*names):
        return next((number(usage[name]) for name in names if number(usage.get(name)) is not None), None)
    return dict(available=bool(usage), input_tokens=field('input_tokens', 'prompt_tokens'),
                output_tokens=field('output_tokens', 'completion_tokens'),
                cache_read_input_tokens=field('cache_read_input_tokens'),
                cache_creation_input_tokens=field('cache_creation_input_tokens'))


def timing(values):
    known = [v for v in values if number(v) is not None]
    return dict(known_records=len(known), missing_records=len(values)-len(known),
        sum_seconds=sum(known), min_seconds=min(known) if known else None,
        median_seconds=statistics.median(known) if known else None,
        max_seconds=max(known) if known else None)


def call_summary(calls):
    usages = [usage_view(c.get('usage')) for c in calls]
    result = dict(calls=len(calls), statuses=dict(Counter(c.get('status', 'unknown') for c in calls)),
        error_types=dict(Counter(c['error_type'] for c in calls if isinstance(c.get('error_type'), str))),
        seconds=timing([c.get('seconds') for c in calls]),
        usage_available_calls=sum(u['available'] for u in usages),
        usage_missing_calls=sum(not u['available'] for u in usages),
        completed_usage_missing_calls=sum(c.get('status') == 'completed' and not u['available']
                                          for c, u in zip(calls, usages)),
        error_usage_missing_calls=sum(c.get('status') == 'error' and not u['available']
                                      for c, u in zip(calls, usages)))
    for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'):
        known = [u[key] for u in usages if u[key] is not None]
        result[key+'_known'] = sum(known)
        result[key+'_missing_calls'] = len(calls)-len(known)
    result['token_totals_complete'] = all(u['input_tokens'] is not None and u['output_tokens'] is not None
                                         for u in usages)
    return result


def stage_view(call, case_id, generation_id, path):
    provider = call.get('provider') if isinstance(call.get('provider'), dict) else {}
    return dict(case_id=case_id, generation_id=generation_id, revision_path=path,
        stage=call.get('stage', 'unknown'), status=call.get('status', provider.get('status', 'unknown')),
        error_type=call.get('error_type', provider.get('error_type')),
        seconds=provider.get('seconds'), usage=provider.get('usage'),
        provider_record_available=bool(provider))


def revision_path(record):
    if record.get('initialization') == 'historical_route_and_p0':
        return 'p0_import'
    if isinstance(record.get('revision_path'), str):
        return record['revision_path']
    stages = {c.get('stage') for c in record.get('calls', [])}
    if 'planner' in stages:
        return 'grounded_legacy_unlabelled'
    if 'attacker_revise' in stages:
        return 'piminer_black_box_legacy_unlabelled'
    return 'unclassified'


def ordinal(episode_id):
    try:
        return int(episode_id.rsplit('/', 1)[1])
    except (AttributeError, IndexError, ValueError):
        return 1 << 30


def preflight_view(run):
    issues = []
    status = read_json(run/'status.json', issues, {})
    saved = read_json(run/'preflight.json', issues)
    if not isinstance(saved, dict):
        return dict(run=str(run), recorded=False, run_status=status.get('status'),
            error_type=status.get('error_type'), durable_target_calls=None,
            durable_attacker_calls=None, cost_recording='unavailable; do not interpret as zero calls or zero cost',
            issues=issues)
    target = saved.get('target', {}).get('calls', [])
    attacker = saved.get('attacker', {})
    sections = {name: section.get('calls', []) for name, section in attacker.items()
                if isinstance(section, dict) and isinstance(section.get('calls'), list)}
    return dict(run=str(run), recorded=True, run_status=status.get('status'),
        target=call_summary(target), attacker_sections={k: call_summary(v) for k, v in sections.items()},
        durable_target_calls=len(target), durable_attacker_calls=sum(len(v) for v in sections.values()),
        issues=issues)


def summarize(run, *, official_summary=None, prior_preflight_runs=()):
    run = Path(run).resolve()
    issues = []
    manifest = read_json(run/'manifest.json', issues)
    if not isinstance(manifest, dict):
        raise ValueError('A readable run manifest is required')
    status = read_json(run/'status.json', issues, {})
    cases = manifest.get('cases', [])
    ids = manifest.get('case_ids', [c['case_id'] for c in cases])
    if (not isinstance(ids, list) or len(set(ids)) != len(ids)
            or any(not isinstance(cid, str) or Path(cid).name != cid or cid in ('.', '..') for cid in ids)):
        raise ValueError('Unsafe or duplicate manifest case IDs')
    snapshots = {c['case_id']: c for c in cases}
    stages, generations, targets, case_rows = [], [], [], []
    recovered_errors, terminal_errors = [], []
    all_started, all_completed, all_gen_started, all_gen_completed = set(), set(), set(), set()
    for cid in ids:
        directory = run/cid
        local_issues = []
        events = read_rows(directory/'events.jsonl', local_issues)
        episodes = read_rows(directory/'episodes.jsonl', local_issues)
        decisions = read_rows(directory/'decisions.jsonl', local_issues)
        episode_by_id = {e.get('episode_id'): e for e in episodes}
        started = {e.get('episode_id') for e in events if e.get('event') == 'TARGET_STARTED'}
        completed = {e.get('episode_id') for e in events if e.get('event') == 'TARGET_COMPLETED'}
        gen_started = {e.get('generation_id') for e in events if e.get('event') == 'GENERATION_STARTED'}
        gen_completed = {e.get('generation_id') for e in events if e.get('event') == 'GENERATION_COMPLETED'}
        all_started.update(started); all_completed.update(completed)
        all_gen_started.update(gen_started); all_gen_completed.update(gen_completed)
        local_gen, local_target = [], []
        for raw_path in sorted((directory/'raw').rglob('*.json')):
            if not raw_path.resolve().is_relative_to(directory.resolve()):
                local_issues.append(dict(code='raw_path_outside_case'))
                continue
            raw = read_json(raw_path, local_issues)
            if not isinstance(raw, dict):
                continue
            if raw_path.name.endswith('.generation.json'):
                gid = raw.get('generation_id')
                expected_gid = raw_path.relative_to(directory/'raw').as_posix()[:-len('.generation.json')]
                if gid != expected_gid or not gid.startswith(cid+'/'):
                    local_issues.append(dict(code='generation_identity_missing_or_foreign'))
                    continue
                path = revision_path(raw)
                calls = [stage_view(c, cid, gid, path) for c in raw.get('calls', []) if isinstance(c, dict)]
                stages.extend(calls)
                row = dict(case_id=cid, generation_id=gid, revision_path=path,
                    status=raw.get('status'), error_type=raw.get('error_type'),
                    completed_event=gid in gen_completed, calls=len(calls),
                    stages=[c['stage'] for c in calls], seconds=timing([c['seconds'] for c in calls]),
                    prompt_mode=raw.get('prompt_mode'),
                    after_target_attempts=(max(raw['history_iters'])+1)
                        if raw.get('history_iters') and all(type(i) is int for i in raw['history_iters']) else None,
                    checker_statuses=[check.get('status', 'unknown') for check in raw.get('checks', [])
                                      if isinstance(check, dict)])
                generations.append(row); local_gen.append(row)
                continue
            result = raw.get('result')
            if not isinstance(result, dict) or not raw_path.stem.isdigit():
                continue
            eid = raw_path.relative_to(directory/'raw').with_suffix('').as_posix()
            if not eid.startswith(cid+'/'):
                local_issues.append(dict(code='target_identity_foreign'))
                continue
            raw_details = result.get('raw') if isinstance(result.get('raw'), dict) else {}
            api = raw_details.get('api_calls')
            api = api if isinstance(api, list) else None
            error = result.get('error')
            gold = result.get('gold') if type(result.get('gold')) is bool and not error else None
            episode = episode_by_id.get(eid, {})
            if episode and episode.get('gold') != result.get('gold'):
                local_issues.append(dict(code='episode_raw_gold_disagree', episode_id=eid))
            api_rows = []
            for index, call in enumerate(api or []):
                view = {k: call.get(k) for k in ('status', 'error_type', 'usage', 'seconds')}
                api_rows.append(view)
                if call.get('status') == 'error':
                    recovered = any(c.get('status') == 'completed' for c in api[index+1:])
                    item = dict(case_id=cid, episode_id=eid, api_call_ordinal=index+1,
                        error_type=call.get('error_type'), recovered_later_in_same_trajectory=recovered,
                        trajectory_gold=gold, trajectory_error_type=error)
                    (recovered_errors if recovered else terminal_errors).append(item)
            row = dict(case_id=cid, episode_id=eid, attempt=ordinal(eid)+1,
                gold=gold, error_type=error, observation_error_type=result.get('observation_error'),
                seconds=result.get('seconds'), trigger=episode.get('trigger'),
                payload_id=episode.get('payload_id'), completed_event=eid in completed,
                api_details_available=api is not None, api_calls=api_rows,
                aggregate_usage=result.get('usage'))
            targets.append(row); local_target.append(row)
        outcomes = read_json(directory/'outcomes.json', local_issues, [])
        outcome = next((o for o in outcomes if o.get('case_id') == cid), {}) if isinstance(outcomes, list) else {}
        child_status = read_json(directory/'status.json', local_issues, {})
        policy_decisions = {d.get('spent_target_budget', -2)+1: d for d in decisions
                            if type(d.get('spent_target_budget')) is int and d.get('action') in ('Retry', 'Revise')}
        first = next((t for t in sorted(local_target, key=lambda t: t['attempt']) if t['gold'] is True), None)
        success = None
        if first:
            index = first['attempt']-1
            decision = policy_decisions.get(index, {})
            label = 'P0' if index == 0 else 'unattributed'
            source_path = None
            if decision.get('action') == 'Retry':
                label = 'T_retry' if decision.get('reason') == 'preserve_observed_trigger' else 'other_retry'
            elif decision.get('action') == 'Revise':
                gid = '/'.join(first['episode_id'].split('/')[:2])+'/'+str(first['payload_id'])
                generation = next((g for g in local_gen if g['generation_id'] == gid), {})
                source_path = generation.get('revision_path')
                label = ('F_grounded_revise' if source_path == 'grounded_false' else
                         'T_cap_black_box_revise' if decision.get('reason') == 'payload_retry_cap' else
                         'U_black_box_revise' if decision.get('reason', '').startswith('unknown_') else
                         'other_black_box_revise')
            success = dict(episode_id=first['episode_id'], attempt=first['attempt'], source=label,
                revision_path=source_path, action=decision.get('action', 'Initial'),
                action_reason=decision.get('reason'))
        observable = bool(snapshots.get(cid, {}).get('goal_spec', {}).get('trigger_ids'))
        disposition = ('confirmed_success' if first else 'complete_failure'
                       if outcome.get('gold_success') is False and child_status.get('status') == 'completed'
                       else 'unknown_error_or_pending')
        case_rows.append(dict(case_id=cid, suite_name=snapshots.get(cid, {}).get('suite_name'),
            observable_trigger=observable, worker_status=child_status.get('status', 'not_started'),
            disposition=disposition, outcome_status=outcome.get('status'), first_success=success,
            target_started=len(started), target_completed=len(completed), durable_target_records=len(local_target),
            unresolved_target_ids=sorted(started-completed), generation_started=len(gen_started),
            generation_completed=len(gen_completed), durable_generation_records=len(local_gen),
            unresolved_generation_ids=sorted(gen_started-gen_completed),
            revision_paths=dict(Counter(g['revision_path'] for g in local_gen)), issues=local_issues))
        issues.extend(dict(case_id=cid, **issue) for issue in local_issues)
    by_path = {}
    for path in sorted({g['revision_path'] for g in generations}):
        gs = [g for g in generations if g['revision_path'] == path]
        cs = [c for c in stages if c['revision_path'] == path]
        by_path[path] = dict(generation_requests=len(gs), statuses=dict(Counter(g['status'] for g in gs)),
            checker_statuses=dict(Counter(status for g in gs for status in g['checker_statuses'])),
            calls=call_summary(cs), by_stage={stage: call_summary([c for c in cs if c['stage'] == stage])
                for stage in sorted({c['stage'] for c in cs})})
    api_calls = [call for target in targets for call in target['api_calls']]
    errors = [dict(case_id=g['case_id'], generation_id=g['generation_id'], revision_path=g['revision_path'],
        revision_id=g['generation_id'].rsplit('/', 1)[-1], after_target_attempts=g['after_target_attempts'],
        status=g['status'], error_type=g['error_type'], seconds=g['seconds'], checker_statuses=g['checker_statuses'],
        stages=[{k: c[k] for k in ('stage', 'status', 'error_type', 'seconds', 'provider_record_available')}
                for c in stages if c['generation_id'] == g['generation_id']])
        for g in generations if g['status'] != 'ready']
    strata = {}
    for name, selected in [('all30', case_rows), ('observable22', [c for c in case_rows if c['observable_trigger']]),
                            ('unobservable8', [c for c in case_rows if not c['observable_trigger']])]:
        strata[name] = dict(assigned_cases=len(selected), confirmed_successes=sum(c['first_success'] is not None for c in selected),
            dispositions=dict(Counter(c['disposition'] for c in selected)),
            first_success_sources=dict(Counter(c['first_success']['source'] for c in selected if c['first_success'])),
            confirmed_success_fraction=(sum(c['first_success'] is not None for c in selected)/len(selected)) if selected else None)
    official = None
    if official_summary is not None:
        summary_path = Path(official_summary).resolve()
        saved = read_json(summary_path, issues)
        if isinstance(saved, dict):
            matches = (Path(saved.get('source', '')).resolve() == run and
                       saved.get('manifest_sha256') == hashlib.sha256((run/'manifest.json').read_bytes()).hexdigest())
            official = dict(path=str(summary_path), source_and_manifest_match=matches,
                status=saved.get('status'), saved_episodes=saved.get('saved_episodes'),
                asr=saved.get('asr') if matches else None,
                current_raw_success_count_matches=(saved.get('asr', {}).get('confirmed_successes')
                                                   == strata['all30']['confirmed_successes']) if matches else None)
    return dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(), run=str(run),
        run_status=status, snapshot_only=status.get('status') not in ('completed', 'completed_with_errors'),
        api_calls_made_by_this_script=0, assigned_cases=len(ids),
        semantics=dict(gold_source='Saved native official result.gold with no Target error; never derived from Trigger, text, plan or checker.',
            pending='Running snapshots include durable records only; started requests without records have unknown cost and are listed.',
            usage='Per-call usage only; resumed model_usage can be cumulative and is never summed. Missing usage is not zero cost.',
            input_tokens='Provider input_tokens/prompt_tokens; cache read/creation reported separately, never silently added.',
            seconds='Sum of known per-stage transport timings excludes gaps and parallel wall time. Target API-call latency is not recorded by this adapter.',
            errors='Error classes retained literally; RuntimeError is never inferred to be TimeoutError.',
            recovered_api_error='A later completed API request exists in the same trajectory; this does not by itself imply Gold success.',
            first_success='Source of the first saved official Gold=True trajectory, not a causal contribution estimate.'),
        by_revision_path=by_path, generation_errors=errors,
        generation_started=len(all_gen_started), generation_completed=len(all_gen_completed),
        unresolved_generation_ids=sorted(all_gen_started-all_gen_completed),
        target=dict(started=len(all_started), completed=len(all_completed), durable_trajectories=len(targets),
            valid_gold_true=sum(t['gold'] is True for t in targets), valid_gold_false=sum(t['gold'] is False for t in targets),
            missing_or_error_gold=sum(t['gold'] is None for t in targets),
            trajectory_errors=dict(Counter(t['error_type'] for t in targets if t['error_type'])),
            observation_errors=dict(Counter(t['observation_error_type'] for t in targets if t['observation_error_type'])),
            seconds=timing([t['seconds'] for t in targets]), api=call_summary(api_calls),
            trajectories_with_api_details=sum(t['api_details_available'] for t in targets),
            trajectories_missing_api_details=sum(not t['api_details_available'] for t in targets),
            aggregate_reported_api_calls_known=sum(t['aggregate_usage'].get('api_calls', 0) for t in targets
                if isinstance(t['aggregate_usage'], dict)),
            recovered_api_error_calls=recovered_errors, unrecovered_api_error_calls=terminal_errors,
            unresolved_target_ids=sorted(all_started-all_completed)),
        strata=strata, official_summary=official, preflight=preflight_view(run),
        prior_preflight_runs=[preflight_view(Path(p).resolve()) for p in prior_preflight_runs],
        cases=case_rows, generation_records=generations,
        issues=issues)


def markdown(report):
    rows = ['# Mixed revision cost snapshot', '',
        f"Run: `{report['run']}`; status: `{report['run_status'].get('status')}`; API calls made by this script: 0.", '',
        'Official success counts come only from saved native Gold. Missing usage and unfinished requests are not zero cost.', '',
        '| Revision path | Requests | Stage calls | Known seconds | Known input tokens | Known output tokens | Missing usage calls |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for path, item in report['by_revision_path'].items():
        cost = item['calls']
        rows.append(f"| {path} | {item['generation_requests']} | {cost['calls']} | {cost['seconds']['sum_seconds']:.2f} | {cost['input_tokens_known']} | {cost['output_tokens_known']} | {cost['usage_missing_calls']} |")
    target = report['target']
    rows += ['', f"Target trajectories: {target['completed']} completed / {target['started']} started; "
        f"{target['api']['calls']} durable API request records, "
        f"{target['api']['statuses'].get('completed', 0)} completed API requests, "
        f"{len(target['recovered_api_error_calls'])} request errors followed by a completed request, "
        f"{len(target['unrecovered_api_error_calls'])} request errors without a later completed request, "
        f"{target['api']['usage_missing_calls']} API requests missing usage.", '',
        '| Stratum | Assigned | Confirmed official successes | First-success sources |', '|---|---:|---:|---|']
    for name, item in report['strata'].items():
        rows.append(f"| {name} | {item['assigned_cases']} | {item['confirmed_successes']} | {json.dumps(item['first_success_sources'], ensure_ascii=False)} |")
    rows += ['', 'Generation failures retain their actual error class and known stage timings in the JSON report. '
        'RuntimeError is not relabelled as a timeout. Preflight records and prior failed preflights are separate from task costs.', '']
    return '\n'.join(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--output-prefix', required=True, type=Path)
    parser.add_argument('--official-summary', type=Path)
    parser.add_argument('--prior-preflight-run', type=Path, action='append', default=[])
    args = parser.parse_args()
    report = summarize(args.run, official_summary=args.official_summary,
                       prior_preflight_runs=args.prior_preflight_run)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    args.output_prefix.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    args.output_prefix.with_suffix('.md').write_text(markdown(report))
    print(json.dumps(dict(run=report['run'], run_status=report['run_status'].get('status'),
        assigned_cases=report['assigned_cases'], revision_paths={k:v['generation_requests'] for k,v in report['by_revision_path'].items()},
        target_completed=report['target']['completed'], target_api_calls=report['target']['api']['calls'],
        strata=report['strata'], generation_errors=len(report['generation_errors']),
        issues=len(report['issues']), snapshot_only=report['snapshot_only'], api_calls=0)))


if __name__ == '__main__':
    main()
