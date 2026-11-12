"""Audit a preregistered subset using the existing complete source/replay checks."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import verify_a2_v2_test30_20261030 as audit
from scripts.verify_piminer_terminal30_20261031 import verify_sources, verify_initial_execution
from scripts.summarize_full_evidence import analyze
from src.engine.config import check_frozen
from src.attacker.initialization import InitializationBundle
from scripts import audit_fresh_initializations as fresh_audit


def verify(run):
    run = Path(run).resolve()
    manifest = audit.read(run/'manifest.json')
    population = check_frozen(manifest)
    selection = manifest['config'].get('experiment', {}).get('assigned_case_ids')
    known = {c['case_id']: c for c in population}
    audit.require(isinstance(selection, list) and selection
                  and all(isinstance(cid,str) and cid in known for cid in selection)
                  and len(set(selection))==len(selection), 'invalid_frozen_selection')
    cases = [known[cid] for cid in selection]
    exp = manifest['config']['experiment']
    source_root = Path(exp['source_snapshot'])
    frozen_source = audit.read(source_root / exp['source_snapshot_manifest'])
    audit.require(Path(frozen_source['snapshot']).resolve()==source_root.resolve(), 'source_root_mismatch')
    for name, expected in frozen_source['files'].items():
        audit.require(hashlib.sha256((source_root/name).read_bytes()).hexdigest()==expected,
                      'frozen_source_changed:'+name)
    audit.require(manifest['cases'] == cases and manifest['case_ids'] == [c['case_id'] for c in cases],
                  'frozen_assigned_subset_mismatch')
    bundle = InitializationBundle(ROOT, manifest['config']['attacker']['initialization_manifest'])
    if bundle.schema_version == 2:
        audit.require(cases == population, 'fresh_audit_requires_entire_cohort')
        source = fresh_audit.verify_sources(ROOT, bundle.path, population,
                                            manifest['config']['target']['model'])
        source_rows = {c['case_id']: dict(case_id=c['case_id'], **bundle.for_case(c)) for c in cases}
    else:
        audit.require([c['case_id'] for c in population] == [f'c{i:03d}' for i in range(30)],
                      'terminal_population_changed')
        source = verify_sources()
        source_rows = {r['case_id']:r for r in source['cases']}
        audit.require(exp['initial_payload_provenance_sha256'] == source['reference_sha256'],
                      'source_sidecar_changed')
    native = analyze(run, verify_completed=True)
    reference = audit.Reconstruction(manifest, run)
    rows = []
    for case in cases:
        cid = case['case_id']
        try:
            initial = (fresh_audit.verify_initial_execution(run, source_rows[cid])
                       if bundle.schema_version == 2
                       else verify_initial_execution(run, source_rows[cid], complete=True))
            row = audit.verify_case(run, case, reference)
            row['initial_source_verification'] = initial
        except Exception as exc:
            row = dict(case_id=cid, status='failed', issues=[dict(code=str(exc), error_type=type(exc).__name__)])
        rows.append(row)
    sessions = [r['session_id'] for r in rows if r.get('session_id')]
    audit.require(len(sessions) == len(set(sessions)), 'cross_case_session_reuse')
    experience = reference.verify_all_experience()
    nonempty = [s for r in rows for s in r.get('experience_snapshots', []) if s['entries']]
    protocol, deviations = audit.experience_protocol(run, manifest['config'], len(nonempty))
    passed = (all(r['status']=='passed' for r in rows)
              and native['verification']['status']=='passed' and not native['issues']
              and experience['status']=='passed' and not deviations)
    return dict(status='passed' if passed else 'failed_or_partial', api_calls=0,
                source_population_cases=len(population), assigned_cases=len(cases),
                selection=manifest['config'].get('experiment',{}).get('assigned_case_ids'),
                source_audit_status=source['status'], source_schema_version=bundle.schema_version,
                source_verification=source, native_verification=native['verification'],
                native_issues=native['issues'], experience_verification=experience,
                experience_protocol=protocol, protocol_deviations=deviations,
                asr=native['asr'], cases=rows)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    path = Path(args.output)
    if path.exists():
        raise FileExistsError(path)
    result = verify(args.run)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='cases'}))
    raise SystemExit(0 if result['status']=='passed' else 1)
