"""Prepare and evaluate controlled mismatch feedback without exposing verdicts."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--dataset', type=Path, default=ROOT / 'Data/mismatch_dataset_v5')
    inspect.add_argument('--output', type=Path, required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--dataset',type=Path,default=ROOT/'Data/mismatch_dataset_v5')
    prepare.add_argument('--p0-dir',type=Path,default=ROOT/'reports/mismatch_p0_20261104')
    prepare.add_argument('--language',choices=['en','zh'],default='en')
    prepare.add_argument('--output',type=Path,required=True)
    run=sub.add_parser('run')
    run.add_argument('--prepared',type=Path,required=True)
    run.add_argument('--output',type=Path,required=True)
    run.add_argument('--method',choices=['piminer','v1'],required=True)
    run.add_argument('--view',choices=['final_only','authorized_evidence'])
    run.add_argument('--model',default='deepseek-flash[1m]')
    run.add_argument('--effort',default='max')
    run.add_argument('--timeout',type=int,default=900)
    run.add_argument('--budget',type=int,default=4)
    run.add_argument('--limit',type=int)
    run.add_argument('--fixture',action='store_true',help='Synthetic engineering smoke; no model calls')
    from ..feedback.ablation import add_arguments
    add_arguments(run)
    run.add_argument('--deepseek-env',type=Path,default=ROOT.parent/'PIMiner/.env')
    score=sub.add_parser('score')
    score.add_argument('--prepared',type=Path,required=True)
    score.add_argument('--run',type=Path,required=True)
    score.add_argument('--output',type=Path,required=True)
    args = parser.parse_args(argv)
    if args.command=='prepare':
        from .data import prepare as prepare_data
        result=prepare_data(args.output,dataset=args.dataset,p0_dir=args.p0_dir,language=args.language)
        print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
        return
    if args.command=='run':
        import os
        from .execution import run_prepared
        from ..feedback.ablation import AssessmentModules
        modules = AssessmentModules(args.ablation, args.judgment_memory)
        decide=None
        if args.fixture:
            from .fixture import decide
        else:
            from ..evidence.predicates.provider import configure_deepseek
            configure_deepseek(args.deepseek_env)
        # Existing durable accounting records usage even when a model call fails.
        os.environ.update(PIMINER_CALL_LOG=str(args.output.resolve()/'provider_calls.jsonl'),
                          PIMINER_STOP_FILE=str(args.output.resolve()/'STOP.json'),PIMINER_RUN_ID=args.output.name)
        result=run_prepared(args.prepared,args.output,method=args.method,feedback_view=args.view,
            model=args.model,effort=args.effort,timeout=args.timeout,budget=args.budget,
            limit=args.limit,decide=decide,fixture=args.fixture,
            ablation=modules.config)
        print(json.dumps(result));return
    if args.command=='score':
        from ..evaluation.mismatch import score as score_run
        result=score_run(args.prepared,args.run,args.output)
        print(json.dumps({k:result[k] for k in ['result_kind','instances','coverage','correct_fraction_all','target_runs']}))
        return
    rows = json.loads((args.dataset / 'response_evaluations.json').read_text())
    result = dict(instances=len(rows), seeds=len({r['seed_id'] for r in rows}),
                  evidence_files=len({r['evidence_sha256'] for r in rows}), model_calls=0)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
