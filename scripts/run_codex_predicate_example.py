#!/usr/bin/env python3
"""Run scene predicate discovery with Codex CLI, retaining raw and parsed output.

Uses current Codex model configuration unless --model is explicitly supplied.
No Target execution. A temporary read-only cwd and disabled integration tools
isolate the model from the workspace; all task data is supplied through stdin.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.evidence.predicates.model import SYSTEM, PROMPTS
from src.evidence.predicates.pipeline import run_pipeline


def object_schema(properties):
    return dict(type='object',properties=properties,required=list(properties),additionalProperties=False)


def schema():
    string={'type':'string'}
    nullable={'anyOf':[string,{'type':'null'}]}
    ref=object_schema(dict(example_id=string,condition_id=string))
    binding=object_schema(dict(query_id=string,operator=string,field=nullable,expected_quote=nullable))
    predicate=object_schema(dict(id=string,concept=string,statement=string,current_quote=string,
                                history_refs=dict(type='array',items=ref),evidence_need=string,
                                binding={'anyOf':[binding,{'type':'null'}]}))
    return object_schema(dict(predicates=dict(type='array',items=predicate,maxItems=12)))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--memory',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--model')
    p.add_argument('--without-history', action='store_true')
    p.add_argument('--timeout',type=int,default=300)
    args=p.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    bundle=json.loads(args.input.read_text())
    memory=[json.loads(l) for l in args.memory.read_text().splitlines() if l.strip()]
    def backend(stage,data):
        if stage!='discover':raise ValueError('Only discovery is supported by this example runner')
        prompt=SYSTEM+'\nDo not call tools or access files. Analyze only INPUT_DATA_JSON.\n'+PROMPTS[stage]+'\nINPUT_DATA_JSON:\n'+json.dumps(data,ensure_ascii=False)
        (output/'prompt.txt').write_text(prompt)
        (output/'generation_input.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
        schema_path=output/'response_schema.json';schema_path.write_text(json.dumps(schema()))
        with tempfile.TemporaryDirectory(prefix='hes-codex-') as cwd:
            command=['codex','exec','--skip-git-repo-check','--ephemeral','--sandbox','read-only',
                     '-C',cwd,'-c','approval_policy="never"','-c','web_search="disabled"',
                     '--json','--color','never','--output-schema',str(schema_path),
                     '--output-last-message',str(output/'raw_response.json')]
            for feature in ['shell_tool','unified_exec','apps','multi_agent','browser_use',
                            'browser_use_external','browser_use_full_cdp_access','computer_use',
                            'in_app_browser','skill_search','code_mode_host']:
                command+=['--disable',feature]
            if args.model:command+=['--model',args.model]
            command+=['-']
            with (output/'codex_events.jsonl').open('w') as stdout,(output/'codex_stderr.log').open('w') as stderr:
                result=subprocess.run(command,input=prompt,text=True,stdout=stdout,stderr=stderr,timeout=args.timeout,cwd=cwd)
            (output/'transport.json').write_text(json.dumps(dict(transport='codex exec',returncode=result.returncode,
                requested_model=args.model or 'configured_default',sandbox='read-only',ephemeral=True),indent=2)+'\n')
            if result.returncode:raise RuntimeError('Codex CLI failed; inspect saved stderr/events')
        return json.loads((output/'raw_response.json').read_text())
    result=run_pipeline(bundle['case'],memory,bundle['episode_id'],bundle['policy'],bundle['packets'],backend=backend,
                        excluded_groups=bundle.get('excluded_groups',[]), use_history=not args.without_history)
    for name in ['retrieval','semantic_predicates','compilation','feedback','manifest']:
        (output/(name+'.json')).write_text(json.dumps(result[name],ensure_ascii=False,indent=2)+'\n')
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(output=str(output),predicates=len(result['semantic_predicates']),
                         bound=len(result['compilation']['candidates']),rejected=len(result['feedback']['rejected_candidates']))))

if __name__=='__main__':main()
