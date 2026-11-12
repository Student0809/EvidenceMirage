"""One dataset claim for a frozen paired holdout, verified by every child.

Uses the same registry as main.py. A claim is never removed on failure: an
interrupted cohort cannot silently become a second heldout evaluation.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path


CLAIM_ENV = ('PIMINER_HELDOUT_CLAIM', 'PIMINER_HELDOUT_ARM', 'PIMINER_HELDOUT_REPEAT')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def heldout_evidence(root, manifest, case_ids):
    """Require frozen provenance evidence, in addition to config.check_frozen."""
    config = manifest['config']
    if config['split'] != 'test':
        return None
    root = Path(root).resolve()
    inputs = manifest['inputs_sha256']
    paths = {}
    for key in ('dataset', 'heldout_audit'):
        path = (root / config[key]).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Heldout inputs must be inside the frozen source root')
        sha = digest(path)
        if inputs.get(str(path.relative_to(root))) != sha:
            raise ValueError('Heldout input hash differs from frozen manifest: ' + key)
        paths[key] = (path, sha)
    audit = json.loads(paths['heldout_audit'][0].read_text())
    if (audit.get('status') != 'complete' or not audit.get('reviewer')
            or not audit.get('selection_rule') or audit.get('independent_heldout') is not True
            or audit.get('dataset_sha256') != paths['dataset'][1]):
        raise ValueError('Heldout independence requires a complete, matching provenance audit')
    dataset_case_ids = [json.loads(line)['case_id'] for line in paths['dataset'][0].read_text().splitlines()
                        if line.strip()]
    if case_ids != audit.get('selected_case_ids') or case_ids != dataset_case_ids:
        raise ValueError('Assigned holdout population differs from the complete ordered dataset/audit')
    return dict(dataset_sha256=paths['dataset'][1], heldout_audit_sha256=paths['heldout_audit'][1],
        independent_heldout=True, audit_reviewer=audit['reviewer'], selection_rule=audit['selection_rule'],
        audit_scope=audit.get('audit_scope'), independence_qualification=audit.get('independence_qualification'))


def frozen_arm(arm):
    return dict(name=arm['name'], source_root=str(Path(arm['source_root']).resolve()),
        frozen=str(Path(arm['frozen']).resolve()), frozen_sha256=arm['frozen_sha256'],
        source_manifest_sha256=arm['source_manifest_sha256'])


def cohort_spec(protocol):
    """Return a test cohort binding; ordinary development needs no claim."""
    manifests = [json.loads(Path(a['frozen']).read_text()) for a in protocol['arms']]
    tests = [m.get('config', {}).get('split') == 'test' for m in manifests]
    if not any(tests):
        return None
    if not all(tests):
        raise ValueError('Cannot mix test and development arms in one cohort')
    evidence = []
    for arm, manifest in zip(protocol['arms'], manifests):
        root = Path(arm['source_root']).resolve()
        snapshot = manifest['config'].get('experiment', {}).get('source_snapshot_manifest')
        if not snapshot or (root / snapshot).resolve() != root / 'source_manifest.json':
            raise ValueError('Heldout requires a frozen source snapshot in every arm')
        evidence.append(heldout_evidence(root, manifest, protocol['case_ids']))
    if len({e['dataset_sha256'] for e in evidence}) != 1:
        raise ValueError('Paired holdout arms must use the same dataset hash')
    return dict(dataset_sha256=evidence[0]['dataset_sha256'], case_ids=protocol['case_ids'],
        repeats=protocol['repeats'], allowed_arms=[frozen_arm(a) for a in protocol['arms']],
        heldout_evidence={a['name']:e for a,e in zip(protocol['arms'], evidence)})


def claim_path(registry_root, dataset_sha256):
    return Path(registry_root).resolve() / 'data/test_evaluations' / (dataset_sha256 + '.json')


def ensure_unclaimed(registry_root, spec):
    if claim_path(registry_root, spec['dataset_sha256']).exists():
        raise ValueError('This test dataset has already been claimed for evaluation')


def claim_cohort(registry_root, protocol, protocol_bytes, spec):
    """Exclusively and durably claim before any arm can run paid preflight."""
    path = claim_path(registry_root, spec['dataset_sha256'])
    path.parent.mkdir(parents=True, exist_ok=True)
    claim = dict(spec, schema_version=1, kind='paired_holdout',
        registry_root=str(Path(registry_root).resolve()), output=str(Path(protocol['output']).resolve()),
        protocol_sha256=hashlib.sha256(protocol_bytes).hexdigest(),
        created_at=datetime.now(timezone.utc).isoformat())
    with path.open('x') as stream:
        json.dump(claim, stream, indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def verify_child_claim(root, frozen, manifest, case_ids, output, environ=None):
    """Validate one authorized arm/repeat before adapters or paid preflight."""
    evidence = heldout_evidence(root, manifest, case_ids)
    if evidence is None:
        return None
    env = os.environ if environ is None else environ
    if any(not env.get(key) for key in CLAIM_ENV):
        raise ValueError('Test execution requires the paired parent cohort claim')
    path = Path(env[CLAIM_ENV[0]]).resolve()
    claim = json.loads(path.read_text())
    if claim.get('schema_version') != 1 or claim.get('kind') != 'paired_holdout':
        raise ValueError('Not a paired holdout cohort claim')
    if path != claim_path(claim['registry_root'], evidence['dataset_sha256']):
        raise ValueError('Cohort claim registry or dataset hash differs')
    if claim['dataset_sha256'] != evidence['dataset_sha256'] or claim['case_ids'] != case_ids:
        raise ValueError('Cohort dataset hash or assigned case population differs')
    protocol_path = Path(claim['output']) / 'protocol.json'
    if digest(protocol_path) != claim['protocol_sha256']:
        raise ValueError('Claimed protocol hash differs')
    protocol = json.loads(protocol_path.read_text())
    if (protocol.get('paid_execution_authorized') is not True
            or str(Path(protocol['output']).resolve()) != claim['output']
            or protocol['case_ids'] != case_ids or protocol['repeats'] != claim['repeats']
            or [frozen_arm(a) for a in protocol['arms']] != claim['allowed_arms']):
        raise ValueError('Claimed protocol bindings differ')
    name, raw_repeat = env[CLAIM_ENV[1]], env[CLAIM_ENV[2]]
    allowed = {a['name']:a for a in claim['allowed_arms']}
    if name not in allowed:
        raise ValueError('Arm is outside the claimed cohort')
    if not raw_repeat.isdecimal() or not 0 <= int(raw_repeat) < claim['repeats']:
        raise ValueError('Repeat is outside the claimed cohort')
    repeat = int(raw_repeat)
    arm = allowed[name]
    root, frozen = Path(root).resolve(), Path(frozen).resolve()
    if (str(root) != arm['source_root'] or str(frozen) != arm['frozen']
            or digest(frozen) != arm['frozen_sha256']
            or digest(root / 'source_manifest.json') != arm['source_manifest_sha256']):
        raise ValueError('Claimed frozen arm or source hash differs')
    if evidence != claim['heldout_evidence'][name]:
        raise ValueError('Claimed heldout provenance evidence differs')
    if Path(output).resolve() != Path(claim['output']) / ('repeat' + str(repeat)) / name:
        raise ValueError('Arm output is outside the claimed repeat')
    return dict(evidence, cohort_claim=str(path), cohort_protocol_sha256=claim['protocol_sha256'],
        cohort_arm=name, cohort_repeat=repeat)


def evaluation_metadata(config, verified_claim=None):
    if config['split'] == 'test':
        if verified_claim is None:
            raise ValueError('Heldout result labeling requires a verified cohort claim')
        return dict(independent_heldout=True, evaluation_scope='independent_heldout',
            heldout_provenance=verified_claim)
    return dict(independent_heldout=False, evaluation_scope=config.get('evaluation_scope', 'development'))
