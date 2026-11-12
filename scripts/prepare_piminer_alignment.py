"""Import original route/P0 into project-local inputs without model/API calls."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.attacker.initialization import (InitializationBundle, canonical, local_path,
    public_identity, read_json, route_digest, sha256_bytes)

DEFAULT_REFERENCE = (ROOT.parent / 'PIMiner/eval_results/pim_test/'
    'deepseek_claude_agentdojo_nano_20261023/iterative_attack_01_agentdojo_gpt-5-nano')
DEFAULT_CASES = 'data/agentdojo_full_observable_20261030/cases.jsonl'
DEFAULT_OUTPUT = 'data/piminer_aligned_20261030'


def prepare(output=DEFAULT_OUTPUT, *, root=ROOT, cases_path=DEFAULT_CASES,
            source_run=DEFAULT_REFERENCE):
    root = Path(root).resolve()
    output = local_path(root, output)
    cases_path = local_path(root, cases_path)
    source_run = Path(source_run).resolve()
    if output.exists():
        raise FileExistsError('Initialization output already exists; use a new version')
    cases_bytes = cases_path.read_bytes()
    cases = [read_json(line) for line in cases_bytes.decode('utf-8').splitlines() if line.strip()]
    case_ids = [case.get('case_id') for case in cases]
    if (not case_ids or any(not isinstance(cid, str) or not re.fullmatch(r'c[0-9]{3}', cid) for cid in case_ids)
            or len(set(case_ids)) != len(case_ids)):
        raise ValueError('Duplicate, missing, or invalid source case IDs')
    if case_ids != sorted(case_ids):
        raise ValueError('Selected cases must retain original source order')
    records, strategy_bytes, audits = [], {}, []
    for case in cases:
        index = int(case['case_id'][1:])
        sample_path = source_run / 'samples' / f'{index:03}.json'
        sample_bytes = sample_path.read_bytes()
        sample = read_json(sample_bytes.decode('utf-8'))
        if (type(sample.get('sample_index')) is not int or sample['sample_index'] != index
                or canonical(public_identity(sample)) != canonical(public_identity(case))):
            raise ValueError('Source public identity or sample index differs from selected case')
        history = sample.get('history')
        if (not isinstance(history, list) or not history or not isinstance(history[0], dict)
                or type(history[0].get('iter')) is not int or history[0]['iter'] != 0):
            raise ValueError('Source must contain the original first attempt (iter=0)')
        first = history[0]
        payload, analysis = first.get('injection'), first.get('analysis')
        if not isinstance(payload, str) or not payload.strip() or not isinstance(analysis, str):
            raise ValueError('Source P0 injection/analysis must be text')
        ids, paths = sample.get('strategy_ids'), sample.get('strategy_paths')
        if (not isinstance(ids, list) or not ids or len(ids) > 3
                or any(not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', sid) for sid in ids)
                or len(set(ids)) != len(ids) or not isinstance(paths, list) or len(paths) != len(ids)):
            raise ValueError('Source ordered route is missing, duplicated, or malformed')
        imported_paths, hashes, source_strategies = [], [], []
        for sid, path in zip(ids, paths):
            if not isinstance(path, str):
                raise ValueError('Source strategy path must be text')
            source_path = (source_run / path).resolve()
            if (not source_path.is_relative_to(source_run / 'strategies')
                    or source_path.name != sid + '.md'):
                raise ValueError('Source strategy path does not match its ordered ID or directory')
            content = source_path.read_bytes()
            content.decode('utf-8')
            if sid in strategy_bytes and strategy_bytes[sid] != content:
                raise ValueError('Conflicting source strategy bytes for the same ID')
            strategy_bytes[sid] = content
            digest = sha256_bytes(content)
            imported_paths.append((output / 'strategies' / (sid + '.md')).relative_to(root).as_posix())
            hashes.append(digest)
            source_strategies.append(dict(id=sid, path=str(source_path), sha256=digest))
        record = dict(case_id=case['case_id'], identity=public_identity(case),
            strategy_ids=deepcopy(ids), strategy_paths=imported_paths, strategy_sha256=hashes,
            route_sha256=route_digest(ids, imported_paths, hashes), payload=payload,
            payload_sha256=sha256_bytes(payload.encode('utf-8')), analysis=analysis,
            analysis_sha256=sha256_bytes(analysis.encode('utf-8')),
            source_metadata=dict(sample_index=index, sample_sha256=sha256_bytes(sample_bytes)))
        records.append(record)
        audits.append(dict(case_id=case['case_id'], sample_path=str(sample_path),
            sample_sha256=sha256_bytes(sample_bytes), strategies=source_strategies,
            payload_sha256=record['payload_sha256'], analysis_sha256=record['analysis_sha256']))
    bundle = dict(schema_version=1, cases_path=cases_path.relative_to(root).as_posix(),
        cases_sha256=sha256_bytes(cases_bytes), case_ids=case_ids, initializations=records)
    audit = dict(schema_version=1, source_run=str(source_run), api_calls=0,
        imported_fields=['public_identity', 'ordered_strategy_ids', 'strategy_bytes',
                         'history[0].injection', 'history[0].analysis'],
        excluded_fields=['historical_target_outcomes', 'later_attempts', 'target_trajectories'],
        case_ids=case_ids, sources=audits)
    (output / 'strategies').mkdir(parents=True)
    for sid, content in sorted(strategy_bytes.items()):
        (output / 'strategies' / (sid + '.md')).write_bytes(content)
    bundle_path = output / 'initializations.json'
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    (output / 'reference.json').write_text(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    loaded = InitializationBundle(root, bundle_path)
    for case in cases:
        loaded.for_case(case)
    return bundle_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=DEFAULT_OUTPUT)
    parser.add_argument('--cases', default=DEFAULT_CASES)
    parser.add_argument('--source-run', type=Path, default=DEFAULT_REFERENCE)
    args = parser.parse_args()
    path = prepare(args.output, cases_path=args.cases, source_run=args.source_run)
    bundle = InitializationBundle(ROOT, path)
    print(json.dumps(dict(status='prepared', cases=len(bundle.case_ids),
        bundle=str(path.relative_to(ROOT)), frozen_inputs=len(bundle.input_paths), api_calls=0)))


if __name__ == '__main__':
    main()
