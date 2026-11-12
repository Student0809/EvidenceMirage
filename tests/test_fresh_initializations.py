from copy import deepcopy
import json
from pathlib import Path
import tempfile
import pytest
from scripts.export_fresh_initializations import export_records, generate_one, audit_record
from src.attacker.initialization import InitializationBundle, sha256_bytes
from src.attacker.original import OriginalPIMinerAdapter
from src.engine.config import ROOT, load_cases, load_config
from src.vendor import router


def record_for_case():
    case=deepcopy(load_cases(load_config(ROOT/'experiments/e0_smoke.yaml'))[0])
    case['case_id']='c000';case['step_index']=case.get('step_index',0)
    case['private_trajectory']='PRIVATE_TRAJECTORY_SENTINEL'
    strategy=router._discover_strategies('agentdojo')[0]['id']
    def decide(prompt,stage):
        assert 'PRIVATE_TRAJECTORY_SENTINEL' not in prompt
        return '<choice>'+strategy+'</choice>' if stage=='router' else '<analysis>Public reasoning</analysis><injection>Mock candidate</injection>'
    adapter=OriginalPIMinerAdapter(ROOT,'fixture','target-fixture',decide=decide)
    return case,generate_one(adapter,case)


def test_fresh_bundle_binds_public_only_generation_and_replays_without_calls():
    case,record=record_for_case()
    assert record['target_executions']==0 and record['history']==[]
    assert 'PRIVATE_TRAJECTORY_SENTINEL' not in json.dumps(record)
    saved=iter(record['generation']['calls'])
    def replay(prompt,stage):
        expected=next(saved)
        assert prompt==expected['prompt'] and stage==expected['stage']
        return expected['output']
    replay.offline_replay=True
    assert audit_record(OriginalPIMinerAdapter(ROOT,'fixture','target-fixture',decide=replay),case,record)['status']=='passed'
    with tempfile.TemporaryDirectory(dir=ROOT/'runs/asr_goal_20261101/offline') as folder:
        root=Path(folder);dataset=root/'cases.jsonl';dataset.write_text(json.dumps(case)+'\n')
        path=export_records(ROOT,dataset,root/'bundle',[record])
        bundle=InitializationBundle(ROOT,path);initial=bundle.for_case(case)
        assert initial['payload']=='Mock candidate'
        assert initial['source_metadata']['kind']=='fresh_public_generation'
        source=ROOT/initial['source_metadata']['record_path']
        source.write_text(source.read_text()+' ')
        with pytest.raises(ValueError,match='changed'):bundle.for_case(case)


def test_fresh_export_rejects_history_failure_and_missing_denominator():
    case,record=record_for_case()
    with tempfile.TemporaryDirectory(dir=ROOT/'runs/asr_goal_20261101/offline') as folder:
        root=Path(folder);dataset=root/'cases.jsonl';dataset.write_text(json.dumps(case)+'\n')
        with pytest.raises(ValueError,match='All assigned'):export_records(ROOT,dataset,root/'missing',[])
        bad=deepcopy(record);bad['history']=[{'security':True}]
        with pytest.raises(ValueError,match='cannot import history'):export_records(ROOT,dataset,root/'history',[bad])
        bad=deepcopy(record);bad['generation']['status']='GENERATION_ERROR'
        with pytest.raises(ValueError,match='incomplete'):export_records(ROOT,dataset,root/'failed',[bad])
        assert not any((root/n).exists() for n in ('missing','history','failed'))


def test_fresh_runtime_rejects_rehashed_private_history_and_schema1_spoof():
    case,record=record_for_case()
    with tempfile.TemporaryDirectory(dir=ROOT/'runs/asr_goal_20261101/offline') as folder:
        root=Path(folder);dataset=root/'cases.jsonl';dataset.write_text(json.dumps(case)+'\n')
        path=export_records(ROOT,dataset,root/'bundle',[record])
        original=json.loads(path.read_text());value=deepcopy(original)
        value['schema_version']=1;path.write_text(json.dumps(value))
        with pytest.raises(ValueError,match='source metadata'):InitializationBundle(ROOT,path)
        value=deepcopy(original);meta=value['initializations'][0]['source_metadata'];source=ROOT/meta['record_path']
        bad=json.loads(source.read_text());bad['history']=[{'raw':'private'}];source.write_text(json.dumps(bad))
        meta['record_sha256']=sha256_bytes(source.read_bytes());path.write_text(json.dumps(value))
        with pytest.raises(ValueError,match='incompatible history'):InitializationBundle(ROOT,path)
