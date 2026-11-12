"""Read-only source and runtime audit for the PIMiner terminal-payload 30-case run.

Historical verdicts are read only by this offline reporter, never by an attacker.
With --run, also execute the existing A2/native/generation audit without APIs.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import verify_a2_v2_test30_20261030 as runtime_audit
from src.attacker.initialization import InitializationBundle, canonical, public_identity
from src.engine.config import check_frozen
from src.vendor.router import _parse_attempt

IDS = [f'c{i:03d}' for i in range(30)]
DEFAULT_REFERENCE = ROOT/'data/piminer_terminal30_20261031/reference.json'


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def verify_entry(case, base, entry, source, sample):
    """Check the last evaluated source record independently of the importer."""
    cid = case['case_id']
    require(source['case_id'] == entry['case_id'] == base['case_id'] == cid, 'source_case_identity')
    require(public_identity(sample) == public_identity(case) == entry['identity'], 'source_public_identity')
    require(type(sample['sample_index']) is int and sample['sample_index'] == int(cid[1:])
            == source['sample_index'] == entry['source_metadata']['sample_index'], 'source_sample_index')
    history = sample['history']
    require(isinstance(history, list) and 1 <= len(history) <= 5
            and [h['iter'] for h in history] == list(range(len(history))), 'source_executed_history')
    last = history[-1]
    require(last['injection'] == entry['payload'], 'not_source_terminal_payload')
    require(entry['analysis'] == '' and entry['analysis_sha256'] == digest(b''), 'historical_analysis_imported')
    require(entry['payload_sha256'] == source['payload_sha256'] == digest(last['injection'].encode()),
            'source_payload_digest')
    require(source['source_terminal_iter'] == last['iter']
            and source['source_history_length'] == len(history), 'source_terminal_position')
    require(source['source_history_sha256'] == digest(canonical(history).encode())
            and source['source_terminal_record_sha256'] == digest(canonical(last).encode()),
            'source_history_digest')
    require(source['source_terminal_analysis_sha256'] == digest(last['analysis'].encode()),
            'source_analysis_digest')
    seconds = last['target_seconds']
    require(last['error'] is None and type(last['security']) is bool and type(last['utility']) is bool
            and type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0
            and isinstance(last['final_output'], str), 'source_terminal_not_evaluated')
    require(source['source_gold'] is last['security'] and source['source_error'] is None
            and source['source_utility'] is last['utility'] and source['source_target_seconds'] == seconds,
            'source_terminal_feedback_sidecar')
    status = sample['status']
    require(status == source['source_status'] and status in ('hit', 'miss'), 'source_status')
    require((status == 'hit' and last['security'] is True
             and not any(h['security'] is True for h in history[:-1]))
            or (status == 'miss' and len(history) == 5
                and all(h['security'] is False for h in history)), 'source_status_history')
    require(history[0]['injection'] == base['payload'] and history[0]['analysis'] == base['analysis'],
            'base_p0_source_mismatch')
    expected = deepcopy(base)
    for name in ('payload', 'payload_sha256', 'analysis', 'analysis_sha256'):
        expected[name] = entry[name]
    require(expected == entry and sample['strategy_ids'] == entry['strategy_ids'], 'route_or_metadata_changed')
    require(source['route_sha256'] == entry['route_sha256']
            and source['original_p0_payload_sha256'] == base['payload_sha256'], 'source_route_or_original_p0_digest')
    require(source['payload_changed_from_original_p0'] is (base['payload'] != entry['payload']),
            'source_changed_marker')
    return dict(case_id=cid, source_status=status, source_terminal_iter=last['iter'],
                payload_sha256=entry['payload_sha256'], analysis_empty=True,
                payload_changed_from_original_p0=base['payload'] != entry['payload'])


def verify_sources(reference_path=DEFAULT_REFERENCE, *, root=ROOT):
    root = Path(root).resolve()
    reference_path = Path(reference_path).resolve()
    sidecar = read(reference_path)
    bundle_path, base_path = root/sidecar['runtime_manifest'], root/sidecar['base_manifest']
    require(digest(bundle_path.read_bytes()) == sidecar['runtime_manifest_sha256'], 'runtime_bundle_digest')
    require(digest(base_path.read_bytes()) == sidecar['base_manifest_sha256'], 'base_bundle_digest')
    require(digest((base_path.parent/'reference.json').read_bytes()) == sidecar['base_reference_sha256'],
            'base_reference_digest')
    bundle, base = read(bundle_path), read(base_path)
    loaded = InitializationBundle(root, bundle_path)
    require(bundle['case_ids'] == base['case_ids'] == sidecar['case_ids'] == IDS, 'exact_original30_population')
    require(sidecar['outcome_filter'] is False, 'historical_outcome_filter')
    require(bundle['cases_path'] == base['cases_path'] == sidecar['cases_path']
            and bundle['cases_sha256'] == base['cases_sha256'] == sidecar['cases_sha256'], 'dataset_changed')
    cases = [json.loads(line) for line in (root/bundle['cases_path']).read_text().splitlines() if line.strip()]
    require([c['case_id'] for c in cases] == IDS, 'dataset_case_order')
    sources = sidecar['sources']
    require([s['case_id'] for s in sources] == IDS, 'source_case_order')
    source_run = Path(sidecar['source_run']).resolve()
    require(digest((source_run/'config.json').read_bytes()) == sidecar['source_config_sha256'],
            'source_config_digest')
    for path, expected in sidecar['source_input_hashes'].items():
        require(digest(Path(path).read_bytes()) == expected, 'historical_source_file_changed')
    audited = []
    for case, old, entry, source in zip(cases, base['initializations'], bundle['initializations'], sources):
        loaded.for_case(case)
        sample_path = source_run/'samples'/f"{int(case['case_id'][1:]):03d}.json"
        require(sample_path == Path(source['sample_path']).resolve(), 'source_sample_path')
        require(digest(sample_path.read_bytes()) == source['sample_sha256']
                == entry['source_metadata']['sample_sha256'], 'source_sample_digest')
        sample = read(sample_path)
        audited.append(verify_entry(case, old, entry, source, sample))
        attempted = sorted((source_run/'attempts').glob(f"{sample['sample_index']:03d}_iter*.txt"))
        expected_paths = [source_run/'attempts'/f"{sample['sample_index']:03d}_iter{h['iter']}.txt"
                          for h in sample['history']]
        require(set(attempted) == set(expected_paths) and len(attempted) == len(expected_paths),
                'unexecuted_or_missing_attempt')
        for history, attempt in zip(sample['history'], expected_paths):
            raw = attempt.read_text()
            analysis, injection = _parse_attempt(raw)
            require(history['raw_attempt'] == raw and history['analysis'] == analysis
                    and history['injection'] == (injection if injection.strip() else raw),
                    'saved_attempt_not_evaluated_payload')
        require(Path(source['source_attempt_path']).resolve() == expected_paths[-1]
                and source['source_attempt_sha256'] == digest(expected_paths[-1].read_bytes()),
                'source_last_attempt_file')
    status_counts = dict(Counter(r['source_status'] for r in audited))
    iter_counts = dict(sorted(Counter(str(r['source_terminal_iter']) for r in audited).items()))
    require(status_counts == sidecar['source_status_counts'] == {'hit': 14, 'miss': 16}, 'source_strata')
    require(iter_counts == sidecar['source_terminal_iter_counts'], 'source_iteration_counts')
    unchanged = [r['case_id'] for r in audited if not r['payload_changed_from_original_p0']]
    require(unchanged == sidecar['payloads_unchanged_from_original_p0']
            and len(audited)-len(unchanged) == sidecar['payloads_changed_from_original_p0'], 'source_changed_counts')
    return dict(status='passed', api_calls=0, reference=str(reference_path),
        reference_sha256=digest(reference_path.read_bytes()), runtime_manifest=str(bundle_path),
        runtime_manifest_sha256=digest(bundle_path.read_bytes()), source_status_counts=status_counts,
        source_terminal_iter_counts=iter_counts, source_files_checked=len(sidecar['source_input_hashes']),
        runtime_analysis_empty_cases=len(audited), cases=audited)


def verify_initial_execution(run, source, *, complete=False):
    cid = source['case_id']
    directory = Path(run)/cid
    events, torn = runtime_audit.rows(directory/'events.jsonl')
    starts = [e for e in events if e['event'] == 'TARGET_STARTED']
    generations = [e for e in events if e['event'] == 'GENERATION_COMPLETED'
                   and e['generation_id'] == f'{cid}/shared/P0']
    require(len(generations) <= 1, 'duplicate_initial_import')
    if not generations:
        require(not complete and not starts, 'missing_initial_import')
        return dict(case_id=cid, status='pending', initial_target_gold=None)
    record = read(runtime_audit.safe_raw(directory, generations[0]['raw_path']))
    require(record['payload_sha256'] == source['payload_sha256']
            and digest(record['payload'].encode()) == source['payload_sha256']
            and record['analysis'] == '' and record['calls'] == []
            and record['initialization'] == 'historical_route_and_p0'
            and record['reused_target_feedback'] is False, 'initial_import_not_source_terminal')
    if not starts:
        require(not complete, 'missing_initial_target_start')
        return dict(case_id=cid, status='pending', initial_target_gold=None)
    first = starts[0]
    require(first['episode_id'] == f'{cid}/shared/000'
            and first['payload_sha256'] == source['payload_sha256']
            and first.get('record_origin', 'new_execution') == 'new_execution', 'initial_target_not_fresh_source_payload')
    episodes, _ = runtime_audit.rows(directory/'episodes.jsonl')
    initial = [e for e in episodes if e['episode_id'] == first['episode_id']]
    require(len(initial) <= 1 and len(starts) <= 5, 'initial_target_identity_or_budget')
    if not initial:
        require(not complete, 'missing_initial_target_completion')
        return dict(case_id=cid, status='pending', initial_target_gold=None)
    episode = initial[0]
    saved = read(runtime_audit.safe_raw(directory, episode['raw_path']))
    require(saved['payload'] == record['payload'] and episode['payload_id'] == 'P0'
            and episode['phase'] == 'initial', 'initial_execution_payload_mismatch')
    return dict(case_id=cid, status='passed' if not torn else 'partial',
                initial_target_gold=episode['gold'], initial_target_status=episode['status'],
                target_starts=len(starts))


def stratify(source_rows, native_rows):
    by_id = {row['case_id']: row for row in native_rows}
    result = {}
    for status in ('hit', 'miss'):
        subset = [source for source in source_rows if source['source_status'] == status]
        rows = [by_id[source['case_id']] for source in subset]
        firsts = [next((e for e in row['timeline'] if e['gold'] is True), None) for row in rows]
        result[status] = dict(assigned_cases=len(rows), confirmed_successes=sum(r['gold_success'] is True for r in rows),
            complete_failures=sum(r['gold_success'] is False for r in rows),
            unresolved_or_error=sum(r['gold_success'] is None for r in rows),
            generation_errors=sum(r.get('terminal_error') == 'GENERATION_ERROR' for r in rows),
            target_errors=sum(r.get('terminal_error') == 'TARGET_ERROR' for r in rows),
            initial_replay_successes=sum(bool(first) and first['action'] == 'Initial' for first in firsts),
            later_revise_successes=sum(bool(first) and first['action'] == 'Revise' for first in firsts),
            later_retry_successes=sum(bool(first) and first['action'] == 'Retry' for first in firsts),
            later_f_evidence_successes=sum(row.get('first_success_revision_path') == 'grounded_false'
                                          and bool(first) and first['action'] == 'Revise'
                                          for row, first in zip(rows, firsts)),
            later_u_or_capped_t_black_box_successes=sum(row.get('first_success_revision_path') == 'piminer_black_box'
                                                       and bool(first) and first['action'] == 'Revise'
                                                       for row, first in zip(rows, firsts)),
            successful_payload_origins=dict(Counter(row.get('successful_payload_origin') for row in rows
                                                     if row['gold_success'] is True)),
            first_success_immediate_actions=dict(Counter(first['action'] for first in firsts if first)),
            case_ids=[source['case_id'] for source in subset])
    return result


def result_rows(run, source_rows, native_rows):
    """Join fresh outcomes with source strata; source Gold never becomes new Gold."""
    by_id = {row['case_id']: row for row in native_rows}
    result = []
    for source in source_rows:
        cid = source['case_id']
        native = by_id[cid]
        first = next((row for row in native['timeline'] if row['gold'] is True), None)
        path = None
        if first:
            if first['action'] == 'Initial':
                path = 'imported_terminal_payload'
            else:
                episodes, _ = runtime_audit.rows(Path(run)/cid/'episodes.jsonl')
                episode = next(row for row in episodes if row['episode_id'] == first['episode_id'])
                payload_id = episode['payload_id']
                if payload_id == 'P0':
                    path = 'imported_terminal_payload'
                else:
                    generation = read(Path(run)/cid/'raw'/cid/'stage_aware'/f'{payload_id}.generation.json')
                    path = generation['revision_path']
        result.append(dict(case_id=cid, source_status=source['source_status'],
            source_recorded_attempts=source['source_terminal_iter']+1,
            new_target_starts=native['target_started'], new_target_completed=native['target_completed'],
            initial_gold=native['timeline'][0]['gold'] if native['timeline'] else None,
            initial_status=native['timeline'][0]['status'] if native['timeline'] else None,
            gold_success=native['gold_success'], disposition=native['disposition'],
            terminal_error=native['terminal_error'], first_success_attempt=first['attempt'] if first else None,
            first_success_action=first['action'] if first else None,
            first_success_revision_path=path, immediate_action=first['action'] if first else None,
            successful_payload_origin=('imported_terminal' if path == 'imported_terminal_payload' else path),
            timeline=native['timeline']))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument('--run', type=Path)
    parser.add_argument('--output-prefix', type=Path, default=ROOT/'reports/piminer_terminal30_independent_source_audit_20261031')
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args(argv)
    prefix = args.output_prefix.resolve()
    require(prefix.is_relative_to(ROOT/'reports'), 'output_must_be_under_reports')
    require(not args.require_complete or args.run, 'require_complete_needs_run')
    report = verify_sources(args.reference)
    report['generated_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    report['scope'] = 'Source terminal payloads and runtime initial slot, with no model APIs.'
    report['comparison_limit'] = ('This run starts from historical terminal candidates and grants up to five new Target '
        'executions. It is not an original equal-budget ASR@5 comparison. Historical hit/miss is a source stratum, '
        'not a new success label; initial replay and later improvements are reported separately. '
        'Historical recorded attempts are saved history entries; legacy in-environment pipeline re-entry means '
        'they are not a verified count of fresh Target trajectories or underlying API requests.')
    exit_code = 0
    if args.run:
        run = args.run.resolve()
        manifest = read(run/'manifest.json')
        check_frozen(manifest)
        config = manifest['config']
        require(config['experiment']['initial_payload_source'] == 'piminer_last_evaluated_attempt', 'wrong_initial_source_protocol')
        require((ROOT/config['attacker']['initialization_manifest']).resolve() == Path(report['runtime_manifest'])
                and config['post_failure_target_budget'] == 4 and not manifest.get('recovery'), 'wrong_terminal_run_inputs_or_budget')
        require(config['experiment']['initial_payload_provenance_sha256'] == report['reference_sha256']
                and (ROOT/config['experiment']['initial_payload_provenance']).resolve() == args.reference.resolve(),
                'frozen_source_sidecar_mismatch')
        runtime_prefix = Path(str(prefix)+'_runtime')
        runtime_args = ['--run', str(run), '--output-prefix', str(runtime_prefix)]
        if args.require_complete:
            runtime_args.append('--require-complete')
        exit_code = runtime_audit.main(runtime_args)
        native = runtime_audit.analyze(run, coverage=ROOT/config['benchmark_audit'])
        runtime = read(runtime_prefix.with_suffix('.json'))
        report['runtime_audit'] = dict(path=str(runtime_prefix.with_suffix('.json')), status=runtime['status'])
        report['initial_executions'] = [verify_initial_execution(run, source, complete=args.require_complete)
                                        for source in report['cases']]
        report['fresh_results'] = result_rows(run, report['cases'], native['cases'])
        report['strata'] = stratify(report['cases'], report['fresh_results'])
        report['generation_error_details'] = []
        source_by_id = {source['case_id']: source for source in report['cases']}
        for case_audit in runtime['cases']:
            cid = case_audit['case_id']
            replay_by_id = {replay['generation_id']: replay for replay in case_audit.get('grounded_replays', [])}
            for generation in case_audit.get('generations', []):
                if generation['status'] != 'GENERATION_ERROR':
                    continue
                gid = generation['generation_id']
                events, _ = runtime_audit.rows(run/cid/'events.jsonl')
                event = next(e for e in events if e['event'] == 'GENERATION_COMPLETED' and e['generation_id'] == gid)
                saved = read(runtime_audit.safe_raw(run/cid, event['raw_path']))
                last = saved['calls'][-1] if saved['calls'] else {}
                report['generation_error_details'].append(dict(case_id=cid, generation_id=gid,
                    source_status=source_by_id[cid]['source_status'], revision_path=saved.get('revision_path'),
                    error_type=saved.get('error_type'), observed_after_stage=last.get('stage'),
                    stage_transport_status=last.get('status'),
                    generation_call_stages=dict(Counter(call['stage'] for call in saved['calls'])),
                    replayed_error_chain=replay_by_id.get(gid, {}).get('error_chain', [])))
        report['generation_errors_by_last_stage'] = dict(Counter(
            error['observed_after_stage'] for error in report['generation_error_details']))
        report['generation_errors_by_source_stratum'] = dict(Counter(
            error['source_status'] for error in report['generation_error_details']))
        report['summary_snapshot'] = {key: native[key] for key in ('run_status', 'asr', 'target_started', 'target_completed')}
        report['status'] = runtime['status']
        if any(r['status'] == 'pending' for r in report['initial_executions']) and report['status'] == 'passed':
            report['status'] = 'partial'
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    lines = ['# Terminal-payload independent audit', '',
             f"Status: {report['status']}; API calls: 0; source cases: 30.", '',
             f"Historical strata: {report['source_status_counts']}; empty initial analysis: 30/30.", '',
             f"Last evaluated iteration counts: {report['source_terminal_iter_counts']}.", '', report['comparison_limit'], '']
    if 'strata' in report:
        lines += ['| Historical source | Assigned | Initial replay success | Later Revise | Later Retry | Complete failure | Error/unresolved |',
                  '|---|---:|---:|---:|---:|---:|---:|']
        for status, row in report['strata'].items():
            lines.append(f"| {status} | {row['assigned_cases']} | {row['initial_replay_successes']} | {row['later_revise_successes']} | {row['later_retry_successes']} | {row['complete_failures']} | {row['unresolved_or_error']} |")
        lines += ['', 'Success attribution retains two axes: immediate action and successful payload origin. '
                  'A Retry of an F-generated candidate counts as a Retry action with grounded_false origin.', '']
        for status, row in report['strata'].items():
            lines.append(f"- Historical {status}: actions {row['first_success_immediate_actions']}; "
                         f"payload origins {row['successful_payload_origins']}.")
        lines += ['', f"Generation errors by last observed stage: {report['generation_errors_by_last_stage']}; "
                  f"by source stratum: {report['generation_errors_by_source_stratum']}. "
                  'These count failed generation records; earlier successful calls in the same case are excluded.']
        lines += ['', f"Full A2/native/generation audit: {report['runtime_audit']['path']}."]
    prefix.with_suffix('.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({key: report[key] for key in ('status', 'api_calls', 'source_status_counts', 'source_terminal_iter_counts')}))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
