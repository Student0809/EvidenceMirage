"""Generate shared public-only route/P0 before any heldout Target execution.

The separately authorized launcher is prepare_fresh_asr_initializations.py.
Export helpers never call Target or infer authorization from an output path.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.attacker.initialization import (InitializationBundle, public_identity,
    route_digest, sha256_bytes, local_path)


def durable_json(path, value):
    """Replace a JSON record atomically and sync both file and directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def export_records(root, cases_path, output, records):
    """Export already generated records; errors cannot be silently dropped."""
    root=Path(root).resolve();cases_path=local_path(root,cases_path);output=local_path(root,output)
    if output.exists():raise FileExistsError(output)
    raw=cases_path.read_bytes();cases=[json.loads(line) for line in raw.splitlines() if line.strip()]
    if [r['case_id'] for r in records]!=[c['case_id'] for c in cases]:
        raise ValueError('All assigned cases must have exactly one initial generation record')
    entries=[];files={}
    for case,record in zip(cases,records):
        if (record['kind']!='fresh_public_generation' or record['identity']!=public_identity(case)
                or record['history']!=[] or record['target_executions']!=0):
            raise ValueError('Fresh initializer cannot import history or another public case')
        generation=record['generation'];route=generation.get('routing',{})
        if generation.get('status')!='ready':
            raise ValueError('Initial generation failed; retain whole assigned cohort as incomplete')
        ids,paths=route['strategy_ids'],route['strategy_paths']
        hashes=[sha256_bytes(local_path(root,p).read_bytes()) for p in paths]
        if route['strategy_sha256']!=dict(zip(paths,hashes)):
            raise ValueError('Generated route bytes changed')
        path=output/'generation'/f"{case['case_id']}.json"
        blob=(json.dumps(record,ensure_ascii=False,indent=2)+'\n').encode()
        files[path]=record
        payload,analysis=generation['payload'],generation['analysis']
        entries.append(dict(case_id=case['case_id'],identity=public_identity(case),strategy_ids=ids,
            strategy_paths=paths,strategy_sha256=hashes,route_sha256=route_digest(ids,paths,hashes),
            payload=payload,payload_sha256=sha256_bytes(payload.encode()),analysis=analysis,
            analysis_sha256=sha256_bytes(analysis.encode()),source_metadata=dict(kind='fresh_public_generation',
                case_id=case['case_id'],record_path=str(path.relative_to(root)),record_sha256=sha256_bytes(blob))))
    bundle=dict(schema_version=2,cases_path=str(cases_path.relative_to(root)),cases_sha256=sha256_bytes(raw),
                case_ids=[c['case_id'] for c in cases],initializations=entries)
    output.mkdir(parents=True,exist_ok=False)
    for path,record in files.items():durable_json(path,record)
    path=output/'initializations.json';durable_json(path,bundle)
    checked=InitializationBundle(root,path)
    for case in cases:checked.for_case(case)
    return path


def generate_one(attacker, case):
    """Original adapter exposes only PUBLIC_FIELDS, with explicitly empty history."""
    payload,generation=attacker.generate_with_history(case,None,None,[],'P0',())
    if generation['payload']!=payload.text or generation['payload_sha256']!=payload.sha256:
        raise ValueError('Generation result/payload differ')
    return dict(schema_version=1,kind='fresh_public_generation',case_id=case['case_id'],
        identity=public_identity(case),generated_at=datetime.now(timezone.utc).isoformat(),
        target_executions=0,history=[],generation=deepcopy(generation))


def audit_record(attacker, case, record):
    """Use an injected replay decision function; no live model may be supplied.

    Callers construct the original adapter with explicit local replay functions
    that assert exact prompt/stage/role and return saved generated outputs.
    """
    if not getattr(attacker.decide,'offline_replay',False) or not getattr(attacker.route_decide,'offline_replay',False):
        raise ValueError('Audit requires explicit offline replay decisions')
    replay=generate_one(attacker,case)['generation']
    expected=record['generation']
    if any(replay.get(k)!=expected.get(k) for k in ('status','payload','payload_sha256','analysis','routing')):
        raise ValueError('Public-only initial-generation replay differs')
    if [(c['prompt'],c['stage'],c['system_prompt'],c['output']) for c in replay['calls']] != [
        (c['prompt'],c['stage'],c['system_prompt'],c['output']) for c in expected['calls']]:
        raise ValueError('Initial prompt differs from public-only reconstruction')
    return dict(status='passed',api_calls=0,case_id=case['case_id'])
