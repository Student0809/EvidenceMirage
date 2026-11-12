"""Import the last executed original PIMiner payload for all fixed 30 cases.

No model calls are made. Runtime fields retain InitializationBundle schema 1;
source outcomes and source iteration numbers are confined to audit sidecars.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.attacker.initialization import (InitializationBundle, canonical, local_path,
    public_identity, read_json, sha256_bytes)

DEFAULT_BASE = 'data/piminer_aligned30_20261030/initializations.json'
DEFAULT_OUTPUT = 'data/piminer_terminal30_20261031'
DEFAULT_REPORT = 'reports/piminer_terminal30_source_20261031'
EXPECTED_CASE_IDS = [f'c{i:03}' for i in range(30)]


def _parse_attempt(raw):
    """Match the original PIMiner submit parser, including its open-tag fallback."""
    injection_match = re.search(r'<injection>(.*?)</injection>', raw, re.DOTALL)
    if injection_match is None:
        injection_match = re.search(r'<injection>(.*)$', raw, re.DOTALL)
    injection = injection_match.group(1) if injection_match else ''
    analysis_match = re.search(r'<analysis>(.*?)</analysis>', raw, re.DOTALL)
    analysis = analysis_match.group(1).strip() if analysis_match else ''
    if not injection.strip():
        injection = raw
    return analysis, injection


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n')


def prepare(output=DEFAULT_OUTPUT, *, root=ROOT, base_manifest=DEFAULT_BASE,
            report_prefix=DEFAULT_REPORT):
    root = Path(root).resolve()
    output = local_path(root, output)
    base_path = local_path(root, base_manifest)
    report_prefix = local_path(root, report_prefix)
    report_json = Path(str(report_prefix) + '.json')
    report_md = Path(str(report_prefix) + '.md')
    if output.exists() or report_json.exists() or report_md.exists():
        raise FileExistsError('Terminal import outputs already exist; use a new version')
    base_bytes = base_path.read_bytes()
    base = read_json(base_bytes.decode('utf-8'))
    old_bundle = InitializationBundle(root, base_path)
    if list(old_bundle.case_ids) != EXPECTED_CASE_IDS:
        raise ValueError('Terminal import requires the exact original c000..c029 population')
    reference_path = base_path.parent / 'reference.json'
    reference_bytes = reference_path.read_bytes()
    reference = read_json(reference_bytes.decode('utf-8'))
    source_run = Path(reference['source_run']).resolve()
    source_config_path = source_run / 'config.json'
    source_config_bytes = source_config_path.read_bytes()
    source_config = read_json(source_config_bytes.decode('utf-8'))
    if (source_config.get('dataset') != 'agentdojo'
            or source_config.get('max_iters') != 5
            or source_config.get('threat_model') != 'black_box'):
        raise ValueError('Unexpected original PIMiner source protocol')
    cases = [read_json(line) for line in (root / base['cases_path']).read_text().splitlines()
             if line.strip()]
    audit_sources = {entry['case_id']: entry for entry in reference['sources']}
    records, sources = [], []
    status_counts, iteration_counts = Counter(), Counter()
    unchanged, source_input_hashes = [], {}
    for case, original in zip(cases, base['initializations']):
        cid = case['case_id']
        old_bundle.for_case(case)
        index = original['source_metadata']['sample_index']
        sample_path = source_run / 'samples' / f'{index:03}.json'
        sample_bytes = sample_path.read_bytes()
        sample_hash = sha256_bytes(sample_bytes)
        if (sample_hash != original['source_metadata']['sample_sha256']
                or sample_hash != audit_sources[cid]['sample_sha256']
                or sample_path != Path(audit_sources[cid]['sample_path']).resolve()):
            raise ValueError(f'{cid}: original sample provenance changed')
        sample = read_json(sample_bytes.decode('utf-8'))
        if (type(sample.get('sample_index')) is not int or sample['sample_index'] != index
                or canonical(public_identity(sample)) != canonical(public_identity(case))):
            raise ValueError(f'{cid}: source public identity differs')
        if sample.get('strategy_ids') != original['strategy_ids']:
            raise ValueError(f'{cid}: original route IDs changed')
        if (not isinstance(sample.get('strategy_paths'), list)
                or len(sample['strategy_paths']) != len(original['strategy_paths'])):
            raise ValueError(f'{cid}: source route paths changed')
        for sid, path, expected_hash, audit_strategy in zip(
                original['strategy_ids'], sample['strategy_paths'],
                original['strategy_sha256'], audit_sources[cid]['strategies']):
            source_strategy = (source_run / path).resolve()
            if (not source_strategy.is_relative_to(source_run / 'strategies')
                    or source_strategy.name != sid + '.md'
                    or str(source_strategy) != audit_strategy['path']
                    or audit_strategy['id'] != sid
                    or sha256_bytes(source_strategy.read_bytes()) != expected_hash
                    or audit_strategy['sha256'] != expected_hash):
                raise ValueError(f'{cid}: original strategy snapshot changed')
            source_input_hashes[str(source_strategy)] = expected_hash
        history = sample.get('history')
        if (not isinstance(history, list) or not 1 <= len(history) <= 5
                or any(not isinstance(h, dict) or type(h.get('iter')) is not int
                       for h in history)
                or [h['iter'] for h in history] != list(range(len(history)))):
            raise ValueError(f'{cid}: execution history is missing or not contiguous')
        if (history[0].get('injection') != original['payload']
                or history[0].get('analysis') != original['analysis']):
            raise ValueError(f'{cid}: original P0 identity differs')
        attempt_paths = list((source_run / 'attempts').glob(f'{index:03}_iter*.txt'))
        attempt_iters = []
        for path in attempt_paths:
            match = re.fullmatch(r'\d{3}_iter(\d+)\.txt', path.name)
            if match is None:
                raise ValueError(f'{cid}: unexpected attempt filename')
            attempt_iters.append(int(match.group(1)))
        if sorted(attempt_iters) != [h['iter'] for h in history]:
            raise ValueError(f'{cid}: unexecuted candidate or missing source attempt; inspect manually')
        for h in history:
            attempt_path = source_run / 'attempts' / f"{index:03}_iter{h['iter']}.txt"
            attempt_bytes = attempt_path.read_bytes()
            raw = attempt_bytes.decode('utf-8')
            if h.get('raw_attempt') != raw or _parse_attempt(raw) != (
                    h.get('analysis'), h.get('injection')):
                raise ValueError(f'{cid}: executed payload/analysis differs from submitted candidate')
            source_input_hashes[str(attempt_path)] = sha256_bytes(attempt_bytes)
        last = history[-1]
        seconds = last.get('target_seconds')
        if (last.get('error') is not None or type(last.get('security')) is not bool
                or type(last.get('utility')) is not bool
                or not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
                or not math.isfinite(seconds) or seconds <= 0
                or not isinstance(last.get('final_output'), str)):
            raise ValueError(f'{cid}: last record does not show a completed target execution')
        status = sample.get('status')
        if ((status == 'hit' and (last['security'] is not True
                                 or any(h.get('security') is True for h in history[:-1])))
                or (status == 'miss' and (len(history) != 5
                                         or any(h.get('security') is not False for h in history)))
                or status not in ('hit', 'miss')):
            raise ValueError(f'{cid}: source terminal status and execution history disagree')
        payload, source_analysis = last.get('injection'), last.get('analysis')
        if not isinstance(payload, str) or not payload.strip() or not isinstance(source_analysis, str):
            raise ValueError(f'{cid}: terminal payload/analysis is invalid')
        entry = deepcopy(original)
        # Only the requested payload transfers. A saved analysis can reveal
        # historical verdicts/final output and is therefore never imported.
        for key, value in (('payload', payload), ('analysis', '')):
            entry[key] = value
            entry[key + '_sha256'] = sha256_bytes(value.encode('utf-8'))
        records.append(entry)
        if entry['payload_sha256'] == original['payload_sha256']:
            unchanged.append(cid)
        status_counts[status] += 1
        iteration_counts[str(last['iter'])] += 1
        attempt_path = source_run / 'attempts' / f"{index:03}_iter{last['iter']}.txt"
        sources.append(dict(case_id=cid, sample_index=index, sample_path=str(sample_path),
            sample_sha256=sample_hash, source_status=status, source_gold=last['security'],
            source_utility=last['utility'], source_terminal_iter=last['iter'],
            source_history_length=len(history), source_target_seconds=seconds,
            source_error=last['error'], source_attempt_path=str(attempt_path),
            source_attempt_sha256=source_input_hashes[str(attempt_path)],
            source_attempt_iters=sorted(attempt_iters), unexecuted_candidates=[],
            source_history_sha256=sha256_bytes(canonical(history).encode('utf-8')),
            source_terminal_record_sha256=sha256_bytes(canonical(last).encode('utf-8')),
            source_terminal_analysis_sha256=sha256_bytes(source_analysis.encode('utf-8')),
            original_p0_payload_sha256=original['payload_sha256'],
            payload_sha256=entry['payload_sha256'], analysis_sha256=entry['analysis_sha256'],
            route_sha256=entry['route_sha256'],
            payload_changed_from_original_p0=entry['payload_sha256'] != original['payload_sha256']))
        source_input_hashes[str(sample_path)] = sample_hash
    bundle = deepcopy(base)
    bundle['initializations'] = records
    audit = dict(schema_version=1, created_at_utc=datetime.now(timezone.utc).isoformat(),
        status='prepared_offline', api_calls=0, source_run=str(source_run),
        source_config_sha256=sha256_bytes(source_config_bytes),
        base_manifest=str(base_path.relative_to(root)), base_manifest_sha256=sha256_bytes(base_bytes),
        base_reference_sha256=sha256_bytes(reference_bytes),
        cases_path=base['cases_path'], cases_sha256=base['cases_sha256'],
        case_ids=EXPECTED_CASE_IDS, selection='history[-1], the last actually executed attempt for each case',
        outcome_filter=False, source_status_counts=dict(status_counts),
        source_terminal_iter_counts=dict(sorted(iteration_counts.items())),
        payloads_changed_from_original_p0=30 - len(unchanged),
        payloads_unchanged_from_original_p0=unchanged,
        source_terminal_errors=0, unexecuted_candidates=0,
        source_input_hashes=source_input_hashes,
        runtime_imported_fields=['public_identity', 'original_fixed_route',
                                 'history[-1].injection',
                                 'sample_index', 'sample_sha256'],
        runtime_excluded_structured_fields=['security', 'utility', 'status', 'iter',
            'history', 'final_output', 'model_output_text', 'trajectory', 'error', 'target_seconds'],
        runtime_analysis_policy='empty string for every case; no source analysis imported',
        analysis_visibility_note=('The saved terminal analysis may describe historical feedback. '
            'It is excluded completely from the runtime bundle and retained only as a SHA-256 '
            'in this audit sidecar, never as text.'),
        invariants=dict(same_30_cases=True, source_samples_match_original_p0_hashes=True,
            original_route_and_strategy_paths_unchanged=True,
            source_attempts_match_all_executed_history=True,
            only_payload_analysis_and_their_hashes_changed=True), sources=sources)
    # Validate every external source a second time before any new artifacts exist.
    for path, expected_hash in source_input_hashes.items():
        if sha256_bytes(Path(path).read_bytes()) != expected_hash:
            raise ValueError('Source changed during import')
    if (sha256_bytes(base_path.read_bytes()) != audit['base_manifest_sha256']
            or sha256_bytes(reference_path.read_bytes()) != audit['base_reference_sha256']
            or sha256_bytes(source_config_path.read_bytes()) != audit['source_config_sha256']):
        raise ValueError('Source manifest changed during import')
    output.mkdir(parents=True)
    bundle_path = output / 'initializations.json'
    _write_json(bundle_path, bundle)
    loaded = InitializationBundle(root, bundle_path)
    for case in cases:
        loaded.for_case(case)
    audit['runtime_manifest'] = bundle_path.relative_to(root).as_posix()
    audit['runtime_manifest_sha256'] = sha256_bytes(bundle_path.read_bytes())
    audit['runtime_frozen_input_count'] = len(loaded.input_paths)
    _write_json(output / 'reference.json', audit)
    report_json.parent.mkdir(parents=True, exist_ok=True)
    _write_json(report_json, audit)
    rows = ['| Case | Original status | Last executed iter | Payload changed |',
            '| --- | --- | ---: | --- |']
    rows.extend(f"| {s['case_id']} | {s['source_status']} | {s['source_terminal_iter']} | "
                f"{'yes' if s['payload_changed_from_original_p0'] else 'no'} |" for s in sources)
    report_md.write_text(
        '# Original PIMiner terminal-payload provenance\n\n'
        'All original c000–c029 cases are retained, without filtering by original outcome. '
        f"Original outcomes: {status_counts['hit']} hit and {status_counts['miss']} miss. "
        'These outcomes describe the historical source only; they are not new evaluation results.\n\n'
        f"Exactly {30 - len(unchanged)} payloads change from the previous P0 import; "
        f'{len(unchanged)} cases originally succeeded at iter 0 and remain identical. '
        'Last executed iteration distribution: ' + json.dumps(dict(sorted(iteration_counts.items()))) + '.\n\n'
        'Every original sample SHA-256 matches the previous P0 bundle. Every submitted attempt '
        'file matches its executed history entry, and no extra unexecuted candidate exists. '
        'All terminal source executions have error=null. Original route IDs, strategy paths, '
        'strategy hashes, dataset bytes and ordering are unchanged. No API was called.\n\n'
        'The runtime manifest retains schema 1 and changes only payload, analysis and their '
        'hashes. Runtime analysis is the empty string for all 30 cases. Source iteration, Gold, '
        'status and execution evidence are confined to the audit sidecar. The source terminal '
        'analysis is excluded because it can reveal historical feedback; only its SHA-256 is '
        'recorded in the sidecar. No source history, final output or tool observations are loaded.\n\n'
        f"Runtime manifest: `{audit['runtime_manifest']}`. "
        f"SHA-256: `{audit['runtime_manifest_sha256']}`.\n\n" + '\n'.join(rows) + '\n')
    return bundle_path, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=DEFAULT_OUTPUT)
    parser.add_argument('--base-manifest', default=DEFAULT_BASE)
    parser.add_argument('--report-prefix', default=DEFAULT_REPORT)
    args = parser.parse_args()
    path, audit = prepare(args.output, base_manifest=args.base_manifest,
                          report_prefix=args.report_prefix)
    print(json.dumps(dict(status=audit['status'], cases=len(audit['case_ids']),
        manifest=str(path.relative_to(ROOT)), source_status_counts=audit['source_status_counts'],
        terminal_iter_counts=audit['source_terminal_iter_counts'],
        changed_payloads=audit['payloads_changed_from_original_p0'], api_calls=0)))


if __name__ == '__main__':
    main()
