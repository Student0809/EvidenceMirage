"""Materialize the audited unused public cases/specs without generation or labels."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.prepare_full_agentdojo import public_spec, reference_templates, native_goal_lookup
from src.decision_learning.preparation import PUBLIC_FIELDS
from src.vendor.a2_selection import TASK_TYPES
from src.attacker.a2 import public_case_sha256
from src.adapters.agentdojo import validate_native_cases

# Frozen taxonomy applied to public user intent only. No attack outcomes exist.
LABELS={
 'native_0892':'financial_operation','native_0897':'information_retrieval',
 'native_0986':'financial_operation','native_0996':'financial_operation',
 'native_1025':'financial_operation','native_0906':'financial_operation',
 'native_0929':'financial_operation','native_0932':'financial_operation',
 'native_0942':'financial_operation','native_0950':'information_retrieval',
 'native_0962':'information_retrieval','native_0974':'financial_operation',
 'native_0824':'message_operation','native_0884':'message_operation',
 'native_0763':'message_operation','native_1035':'booking_operation',
 'native_1042':'calendar_operation','native_1117':'information_retrieval',
 'native_1147':'information_retrieval','native_1163':'information_retrieval',
 'native_1205':'information_retrieval','native_1056':'information_retrieval',
 'native_1077':'information_retrieval','native_1084':'information_retrieval',
 'native_0122':'calendar_operation'}


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def rows(path):return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def prepare(reservation_path, output):
    reservation_path=Path(reservation_path).resolve();output=Path(output).resolve()
    if not output.is_relative_to(ROOT) or output.exists():raise ValueError('Need a new project-local output')
    reservation=json.loads(reservation_path.read_text())
    followup=Path(reservation['prior_use_followup'])
    if sha(followup)!=reservation['prior_use_followup_sha256']:raise ValueError('Prior-use evidence changed')
    config=json.loads((ROOT/'configs/a2_v2_plan_memory_test30_20261031_224926.lock.json').read_text())['config']
    library=json.loads((ROOT/config['attacker']['a2_library']).read_text())
    strategy_paths=sorted(library['source_sha256'])
    for path,digest in library['source_sha256'].items():
        if sha(ROOT/path)!=digest:raise ValueError('Frozen strategy bytes changed')
    strategy_texts=[(ROOT/path).read_text() for path in strategy_paths]
    templates=reference_templates(rows(ROOT/config['dataset']),rows(ROOT/config['goal_specs']))
    native_goals=native_goal_lookup();cases=[];specs=[];mapping=[]
    for index,entry in enumerate(reservation['cases']):
        source=ROOT/entry['source_path']
        if sha(source)!=entry['source_file_sha256']:raise ValueError('Reserved native source changed')
        raw=source.read_bytes().splitlines()[entry['source_line']-1]
        if hashlib.sha256(raw).hexdigest()!=entry['source_line_sha256']:raise ValueError('Reserved source row changed')
        original=json.loads(raw)
        if original['case_id']!=entry['case_id']:raise ValueError('Source identity changed')
        case={k:deepcopy(original[k]) for k in PUBLIC_FIELDS if k in original}
        if any(case['user_task_prompt'] in text or case['context_with_placeholder'] in text for text in strategy_texts):
            raise ValueError('Holdout overlaps frozen strategy examples')
        case.update(case_id=f'c{index:03}',split='test',group_id=original['base_task_id'],strategy_paths=strategy_paths,
            provenance=dict(source='audited_native_public_reservation',source_native_case_id=entry['case_id'],
                source_line=entry['source_line'],source_line_sha256=entry['source_line_sha256'],
                previously_evaluated=False,independence='qualified_unused_for_project_tuning_within_inspected_local_records',
                reservation_sha256=sha(reservation_path),prior_use_followup_sha256=sha(followup)))
        spec,origin,reason=public_spec(case,templates,native_goals)
        if spec is None:raise ValueError('Reserved observable goal unavailable: '+str(reason))
        cases.append(case);specs.append(spec)
        mapping.append(dict(case_id=case['case_id'],source_native_case_id=entry['case_id'],base_task_id=case['base_task_id'],
            source_line=entry['source_line'],source_line_sha256=entry['source_line_sha256'],specification_origin=origin))
    if len({c['base_task_id'] for c in cases})!=len(cases):raise ValueError('Repeated heldout base')
    validation=validate_native_cases(cases,'v1.2.2')
    output.mkdir(parents=True,exist_ok=False)
    for name,values in [('cases',cases),('goal_specs',specs)]:
        (output/(name+'.jsonl')).write_text(''.join(json.dumps(v,ensure_ascii=False)+'\n' for v in values))
    dataset=output/'cases.jsonl';digest=sha(dataset)
    labels=[dict(case_id=c['case_id'],group=['agentdojo',c['suite_name'],c['user_task_id']],
        task_type=LABELS[m['source_native_case_id']],public_sha256=public_case_sha256(c),
        source=str(dataset.relative_to(ROOT)),source_line=i+1,source_sha256=digest,
        reviewer='codex_root_public_user_intent_review_20261101',evidence=c['user_task_prompt'],
        note='Task-type metadata only; frozen taxonomy, no outcome labels or prompt tuning.')
        for i,(c,m) in enumerate(zip(cases,mapping))]
    (output/'task_annotations.json').write_text(json.dumps(dict(schema_version=1,task_types=TASK_TYPES,labels=labels),ensure_ascii=False,indent=2)+'\n')
    metadata=dict(status='prepared_awaiting_runtime_review',api_calls=0,target_executions=0,
        native_validation=validation,dataset_sha256=digest,goal_specs_sha256=sha(output/'goal_specs.jsonl'),
        reservation=str(reservation_path),reservation_sha256=sha(reservation_path),mapping=mapping,
        selection_rule=reservation['selection_rule'],independence=cases[0]['provenance']['independence'])
    (output/'preparation.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
    return dict(output=str(output),assigned_cases=len(cases),api_calls=0,native_validation=validation)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--reservation',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(json.dumps(prepare(a.reservation,a.output)))
