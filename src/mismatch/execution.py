"""Run public inputs only. Hidden scoring is a separate command and process."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from ..attacker.original import OriginalPIMinerAdapter
from ..attacker.piminer import PIMinerAdapter
from ..engine.feedback_runner import FeedbackRunner
from ..feedback.schema import PROTOCOL, VIEWS
from ..feedback.ablation import AssessmentModules
from .data import ROOT, digest, jsonl, load_attempt, write_json, write_jsonl


def run_prepared(prepared, output, *, method, feedback_view=None, model='deepseek-flash[1m]',
                 effort='max', timeout=900, budget=4, limit=None, decide=None, fixture=False,
                 ablation=None, judgment_memory=None):
    modules = AssessmentModules(ablation, judgment_memory)
    if method not in ('piminer','v1'):
        raise ValueError('Unknown autonomous method')
    if type(budget) is not int or budget < 0:
        raise ValueError('Nonnegative target budget required')
    if fixture and decide is None:
        from .fixture import decide
    view = feedback_view or modules.config.feedback_view
    if view not in VIEWS:
        raise ValueError('Unknown feedback view')
    if view != modules.config.feedback_view:
        raise ValueError('Feedback view conflicts with ablation group; select A0 for final_only')
    prepared, output = Path(prepared), Path(output)
    manifest = json.loads((prepared / 'manifest.json').read_text())
    inputs = prepared / 'model_inputs.jsonl'
    if manifest['protocol'] != PROTOCOL or digest(inputs) != manifest['files']['model_inputs.jsonl']:
        raise ValueError('Public input file differs from prepared manifest')
    rows = jsonl(inputs)
    if len(rows) != manifest['instances'] or len({r['uid'] for r in rows}) != len(rows):
        raise ValueError('Public population differs from prepared manifest')
    if limit is not None:
        if type(limit) is not int or not 1 <= limit <= len(rows):
            raise ValueError('Limit outside prepared population')
        rows = rows[:limit]
    if output.exists():
        raise ValueError('Run directory exists; no automatic resampling or overwrite')
    output.mkdir(parents=True)
    run_manifest = dict(protocol=PROTOCOL, method=method, feedback_view=view,
        model='fixture' if fixture else model, effort=effort, instances=len(rows), uids=[r['uid'] for r in rows],
        target_budget=budget, public_inputs_sha256=digest(inputs),
        result_kind='SYNTHETIC_SMOKE' if fixture else 'CONTROLLED_STATIC_FEEDBACK',
        cross_instance_experience=modules.config.historical_experience, target_runs=0,
        ablation=modules.metadata())
    write_json(output / 'manifest.json', run_manifest)
    predictions = []
    write_json(output / 'status.json',dict(status='running',completed=0,total=len(rows)))
    try:
        for row in rows:
            # Each instance gets a fresh adapter and stateless, full-history CLI calls.
            options = dict(decide=decide, ablation=modules.config)
            attacker = (OriginalPIMinerAdapter(ROOT,model,'gpt-5-nano',effort=effort,timeout=timeout,**options)
                        if method=='piminer' else PIMinerAdapter(ROOT,model,effort,timeout,**options))
            model_decide = attacker.autonomous_decide if method=='piminer' else attacker.decide
            trace = []

            def tracked(prompt, stage):
                item=dict(stage=stage,prompt=prompt,status='running')
                trace.append(item)
                write_json(output / 'cases' / f'{row["uid"]}.calls.json', trace)
                before=len(getattr(model_decide,'calls',[]));start=time.monotonic()
                try:
                    value=model_decide(prompt,stage)
                    item.update(status='completed',output=value)
                    return value
                except Exception as exc:
                    item.update(status='error',error_type=type(exc).__name__)
                    raise
                finally:
                    item['seconds']=time.monotonic()-start
                    calls=getattr(model_decide,'calls',[])
                    if len(calls)>before:
                        item['provider']=calls[-1]
                    write_json(output / 'cases' / f'{row["uid"]}.calls.json',trace)

            if method=='piminer':attacker.autonomous_decide=tracked
            else:attacker.decide=tracked
            def persist(result):
                write_json(output / 'cases' / f'{row["uid"]}.json',asdict(result))
            result=FeedbackRunner(attacker,feedback_view=view,target_budget=budget,persist=persist).run(load_attempt(row))
            latest=result.decisions[-1] if result.decisions else None
            provider=[item.get('provider',{}) for item in trace]
            usage={}
            for key in ('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens'):
                values=[p.get('usage',{}).get(key) if isinstance(p.get('usage'),dict) else None for p in provider]
                usage[key]=sum(values) if values and all(type(v) in (int,float) for v in values) else None
                usage['known_'+key]=sum(v for v in values if type(v) in (int,float))
            costs=[p.get('reported_cost_usd') for p in provider]
            reported_cost=sum(costs) if costs and all(type(v) in (int,float) for v in costs) else None
            prediction=dict(uid=row['uid'],status=result.status,assessment=latest,
                ablation=result.ablation,
                action=latest['action'] if latest else None,error_type=result.error_type,
                target_attempts=result.target_attempts,model_calls=len(trace),
                p0_sha256=load_attempt(row).payload.sha256,
                usage=usage,reported_cost_usd=reported_cost,
                known_reported_cost_usd=sum(v for v in costs if type(v) in (int,float)),
                slot_validity=[g.get('slot_validation',{}).get('status','not_checked') for g in result.generations],
                goal_literal_diagnostics=[g['slot_validation']['goal_literal_diagnostic'] for g in result.generations
                    if g.get('slot_validation',{}).get('status')=='passed'],
                candidate_sha256=[g.get('payload_sha256') for g in result.generations if g.get('status')=='ready'])
            predictions.append(prediction)
            write_jsonl(output / 'predictions.jsonl',predictions)
            write_json(output / 'status.json',dict(status='running',completed=len(predictions),total=len(rows)))
        errors=sum(r['status'].endswith('_error') for r in predictions)
        write_json(output / 'status.json',dict(status='completed_with_errors' if errors else 'completed',
            completed=len(predictions),total=len(rows),errors=errors,
            predictions_sha256=digest(output/'predictions.jsonl')))
    except BaseException as exc:
        write_json(output / 'status.json',dict(status='interrupted',completed=len(predictions),
                    total=len(rows),error_type=type(exc).__name__))
        raise
    return dict(output=str(output.resolve()),instances=len(rows),errors=errors,
                result_kind=run_manifest['result_kind'],target_runs=0,ablation=modules.metadata())
