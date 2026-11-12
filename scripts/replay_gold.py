"""Recompute archived native scores; never run a Target or feed snapshots to a policy."""
import argparse
import json
from pathlib import Path
import sys
from typing import get_args
from pydantic import BaseModel, TypeAdapter
import agentdojo.default_suites
from agentdojo.functions_runtime import FunctionCall
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.task_suite.task_suite import model_output_from_messages, functions_stack_trace_from_messages
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.adapters.agentdojo import official_scores
from src.engine.config import digest
from src.engine.storage import plain, write_json


def restore_state(annotation, data):
    """Restore runtime collections after native initialization validators.

    Copied from original feedback_ablation_stage1 verifier. A direct
    model_validate would reset some post-state collections to their initial data.
    """
    value=TypeAdapter(annotation).validate_python(data)
    if isinstance(value,BaseModel):
        for name,field in type(value).model_fields.items():
            if name in data:
                object.__setattr__(value,name,restore_state(field.annotation,data[name]))
    elif isinstance(value,dict):
        args=get_args(annotation)
        if len(args)==2:
            value={k:restore_state(args[1],v) for k,v in data.items()}
    elif isinstance(value,list):
        args=get_args(annotation)
        if len(args)==1:value=[restore_state(args[0],v) for v in data]
    return value


def replay(source):
    cases={c['case_id']:c for c in map(json.loads,(ROOT/'data/dev.jsonl').read_text().splitlines())}
    rows=[]
    for result_path in sorted((Path(source)/'execution').glob('eval_*/result.json')):
        result=json.loads(result_path.read_text());directory=result_path.parent
        if result.get('status')!='valid':
            rows.append(dict(id=directory.name,status=result.get('status'),recomputed=False))
            continue
        case=cases[f"c{result['job']['index']:03d}"]
        suite=get_suite('v1.2.2',case['suite_name'])
        raw=[json.loads((directory/f'{name}.json').read_text()) for name in ('pre','post')]
        pre,post=[restore_state(suite.environment_type,state) for state in raw]
        if [plain(pre),plain(post)]!=raw:
            raise ValueError('State roundtrip mismatch')
        messages=json.loads((directory/'messages.json').read_text())
        for message in messages:
            if message['role']=='assistant':
                message['tool_calls']=[FunctionCall.model_validate(t) for t in message.get('tool_calls') or []]
        utility,gold=official_scores(suite,suite.get_user_task_by_id(case['user_task_id']),
                                     suite.get_injection_task_by_id(case['injection_task_id']),
                                     model_output_from_messages(messages) or [],pre,post,
                                     functions_stack_trace_from_messages(messages))
        if (utility,gold)!=(result['utility'],result['gold']):
            raise ValueError('Official score differs: '+str(directory))
        files=[directory/name for name in ('pre.json','post.json','messages.json','result.json')]
        rows.append(dict(id=directory.name,case_id=case['case_id'],recomputed=True,gold=gold,utility=utility,
                         source_sha256={str(p):digest(p) for p in files}))
    if not rows:raise ValueError('No archived evaluation records found')
    return dict(status='passed',scope='Historical native scorer regression; no new target/model calls; not an adaptive-policy experiment.',
                recomputed=sum(r['recomputed'] for r in rows),gold_successes=sum(r.get('gold') is True for r in rows),rows=rows)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',default=str(ROOT.parent/'PIMiner/eval_results/subgoal_comparison/partial_retry_revise_nano_20261028_v2'))
    p.add_argument('--output',default=str(ROOT/'reports/historical_gold_regression.json'))
    args=p.parse_args();report=replay(args.source);write_json(args.output,report)
    print(json.dumps({k:v for k,v in report.items() if k!='rows'}))
