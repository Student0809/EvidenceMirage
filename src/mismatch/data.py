"""Prepare separate public inputs and private scores for the frozen V5 dataset."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from ..feedback.schema import PublicAttempt, public_case, PROTOCOL
from ..schema import Payload
from ..vendor import router
from .replay_observer import replay_packets

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def write_jsonl(path, rows):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    temporary.replace(path)


def load_attempt(row):
    """The runner reads only this prepared record, never source/annotation paths."""
    return PublicAttempt(public_case(row['case']), row['strategy_document'],
                         Payload('P0', row['injection']), row['analysis'], row['final_output'],
                         tuple(row['packets']))


def prepare(output, *, dataset=None, p0_dir=None, language='en'):
    dataset = Path(dataset or ROOT / 'Data/mismatch_dataset_v5')
    p0_dir = Path(p0_dir or ROOT / 'reports/mismatch_p0_20261104')
    if language not in ('en', 'zh'):
        raise ValueError('Unsupported language')
    output = Path(output)
    if output.exists():
        raise ValueError('Prepared directory exists; choose a fresh output')
    evaluations = json.loads((dataset / 'response_evaluations.json').read_text())
    p0s = jsonl(p0_dir / 'P0.jsonl')
    initial = {r['seed_id']: r for r in p0s}
    if len(evaluations) != 126 or len(initial) != 18 or len(p0s) != 18:
        raise ValueError('Expected frozen V5 126 instances and 18 unique P0 records')
    if len({r['instance_id'] for r in evaluations}) != 126:
        raise ValueError('Duplicate instance identity')
    if set(initial) != {r['seed_id'] for r in evaluations}:
        raise ValueError('P0 seed coverage differs')
    records = {r['record_id']: r for r in json.loads((dataset / 'dataset.json').read_text())}
    sources = {}; publics = []; private = []; mapping = {}
    for i, evaluation in enumerate(evaluations):
        sid = evaluation['seed_id']; p0 = initial[sid]
        case = json.loads((p0_dir / 'inputs' / f'{sid}.json').read_text())
        row = records[evaluation['record_id']]
        if (row['record_id'] not in p0['shared_record_ids']
                or row['seed_id'] != sid
                or any(row[k] != case[k] for k in ('suite_name', 'user_task_prompt', 'injection_task_goal'))
                or p0['injection_candidates'] != case['injection_candidates']
                or Payload('P0', p0['injection']).sha256 != p0['sha256']):
            raise ValueError('P0 identity does not match current mismatch task')
        paths = []
        for relative in p0['routing']['strategy_paths']:
            path = (ROOT / relative).resolve()
            if not path.is_relative_to(ROOT) or digest(path) != p0['routing']['strategy_sha256'][relative]:
                raise ValueError('Selected strategy changed')
            paths.append(str(path))
            sources[str(path)] = digest(path)
        strategies = router._load_sample_strategy(ROOT, dict(strategy_paths=paths), None)
        evidence_path = dataset / 'evidence' / Path(evaluation['evidence_path']).name
        if digest(evidence_path) != evaluation['evidence_sha256']:
            raise ValueError('Frozen evidence changed')
        evidence = json.loads(evidence_path.read_text())
        if evidence['seed_id'] != sid or evidence['branch'] != evaluation['execution_branch']:
            raise ValueError('Evidence identity differs')
        sources[str(evidence_path)] = digest(evidence_path)
        uid = f'i{i:04d}'  # Opaque transport identity, never rendered to the model.
        public = dict(uid=uid, case=public_case(case), strategy_document=strategies,
                      injection=p0['injection'], analysis=p0['analysis'],
                      final_output=evaluation['response'][language],
                      packets=replay_packets(evidence, case['suite_name']))
        load_attempt(public)
        publics.append(public)
        annotation = evaluation['annotation']
        private.append(dict(uid=uid, instance_id=evaluation['instance_id'], seed_id=sid,
                            mode=evaluation['mode'], response_role=evaluation['response_role'],
                            execution_branch=evaluation['execution_branch'],
                            actual_goal_completed=annotation['actual_goal_completed'],
                            actual_progress=evaluation['execution_branch'],
                            claim_supported=annotation['claim_supported'],
                            disclosure=annotation['disclosure'], split_group_id=evaluation['split_group_id'],
                            annotation=deepcopy(annotation), evidence_sha256=evaluation['evidence_sha256']))
        mapping[evaluation['instance_id']] = uid
    pairs = []
    for sid in sorted(initial):
        for mode in ('M2', 'M3'):
            pairs.append(dict(kind='response_intervention', mode=mode, seed_id=sid,
                left=mapping[f'{sid}_{mode}:accurate_response'],
                right=mapping[f'{sid}_{mode}:mismatched_response']))
        pairs.append(dict(kind='disclosure_intervention', mode='M1', seed_id=sid,
            left=mapping[f'{sid}_M1:full'], right=mapping[f'{sid}_M1:accurate_full_control']))
        pairs.append(dict(kind='state_intervention', mode='M1', seed_id=sid,
            left=mapping[f'{sid}_M1:none'], right=mapping[f'{sid}_M1:full']))
    output.mkdir(parents=True)
    write_jsonl(output / 'model_inputs.jsonl', publics)
    write_jsonl(output / 'scoring_private.jsonl', private)
    write_json(output / 'pairing_private.json', pairs)
    for path in [dataset / 'response_evaluations.json', dataset / 'dataset.json', p0_dir / 'P0.jsonl']:
        sources[str(path)] = digest(path)
    manifest = dict(protocol=PROTOCOL, instances=126, seeds=18, evidence_files=len(
        {r['evidence_sha256'] for r in private}), language=language,
        view_policy='permitted_write_receipts_only; no completeness or goal truth',
        source_sha256=sources,
        files={n: digest(output / n) for n in ('model_inputs.jsonl', 'scoring_private.jsonl', 'pairing_private.json')})
    write_json(output / 'manifest.json', manifest)
    return manifest
