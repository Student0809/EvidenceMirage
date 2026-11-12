#!/usr/bin/env python3
"""No-model integration audit on the first saved episode of each supplied case.

Only authorized fixed packets enter the engine. Gold, payloads, private traces
and final-response narratives are never passed to retrieval/generation.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.evidence.observer import FixedObserver, policy_for_case
from src.evidence.predicates.pipeline import run_pipeline
from src.evidence.predicates.engine import digest


def rows(path):return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',type=Path,required=True)
    p.add_argument('--episodes-root',type=Path,required=True)
    p.add_argument('--memory',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    memory=rows(args.memory);results=[];skipped=[]
    for case in rows(args.cases):
        path=args.episodes_root/case['case_id']/'episodes.jsonl'
        episodes=rows(path) if path.exists() else []
        if not episodes or episodes[0].get('observation') is None:
            skipped.append(dict(case_id=case['case_id'],reason='first_episode_observation_missing'))
            continue
        episode=episodes[0]
        packets=list(FixedObserver().collect(episode['episode_id'],case,episode['observation']))
        result=run_pipeline(case,memory,episode['episode_id'],policy_for_case(case),packets)
        result['replay_source']=dict(path=str(path.resolve()),line=1,packet_hash=digest(packets))
        (args.output/(case['case_id']+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        results.append(result)
    judgments=[j for r in results for j in r['feedback']['judgments']]
    requirements=[q for r in results for q in r['compilation']['evidence_requirements']]
    summary=dict(backend='rules-v1',history_records=len(memory),cases=len(results),skipped=skipped,
                 semantic_predicates=sum(len(r['semantic_predicates']) for r in results),
                 bound_candidates=sum(len(r['compilation']['candidates']) for r in results),
                 judgment_counts=dict(Counter(j['verdict'] for j in judgments)),
                 unbound_requirements=len(requirements),
                 rejections=sum(len(r['feedback']['rejected_candidates']) for r in results),
                 interpretation='Integration coverage only; no accuracy or historical-increment claim; first episode per case.')
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
