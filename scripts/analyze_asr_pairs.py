"""Fixed-denominator paired ASR analysis with case-clustered uncertainty."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import statistics


def percentile(values, q):
    index=(len(values)-1)*q;lo=int(index);hi=min(lo+1,len(values)-1)
    return values[lo]+(values[hi]-values[lo])*(index-lo)


def analyze(pairs, case_ids, seed=20261101, bootstrap_samples=10000):
    if not pairs or not case_ids or len(set(case_ids))!=len(case_ids):
        raise ValueError('Need repeats and unique fixed case IDs')
    rounds=[]; deltas={cid:[] for cid in case_ids}
    for reference,candidate in pairs:
        if any(r['assigned_cases']!=len(case_ids) or [c['case_id'] for c in r['cases']]!=case_ids
               for r in (reference,candidate)):
            raise ValueError('Fixed case denominator/order differs')
        row={}
        for name,report in [('reference',reference),('candidate',candidate)]:
            cases=report['cases'];n=len(case_ids)
            row[name]=dict(successes=sum(c['strict_success'] for c in cases),assigned=n,
                initial_successes=sum(c['initial_success'] for c in cases),
                rescued_successes=sum(c['rescued_success'] for c in cases),
                incomplete_cases=sum(not c['complete'] for c in cases),
                generation_error_cases=report['generation_error_cases'],
                complete_asr=report['complete_asr'],
                end_to_end_success_rate=sum(c['strict_success'] for c in cases)/n,
                first_success_sources=dict(Counter(c['first_success']['source'] for c in cases if c.get('first_success'))))
        for r,c in zip(reference['cases'],candidate['cases']):
            deltas[r['case_id']].append(int(c['strict_success'])-int(r['strict_success']))
        row['absolute_gain']=row['candidate']['end_to_end_success_rate']-row['reference']['end_to_end_success_rate']
        rounds.append(row)
    means=[statistics.mean(v) for v in deltas.values()]
    rng=random.Random(seed)
    samples=sorted(statistics.mean(rng.choices(means,k=len(means))) for _ in range(bootstrap_samples))
    baseline=statistics.mean(r['reference']['end_to_end_success_rate'] for r in rounds)
    gain=statistics.mean(means)
    return dict(assigned_cases_per_arm_repeat=len(case_ids),repeats=len(pairs),rounds=rounds,
        mean_reference_end_to_end=baseline,mean_candidate_end_to_end=baseline+gain,
        mean_absolute_gain=gain,relative_gain=gain/baseline if baseline else None,
        repeat_gain_sample_std=statistics.stdev(r['absolute_gain'] for r in rounds) if len(rounds)>1 else None,
        paired_case_gains={cid:dict(repeat_gains=v,mean=statistics.mean(v)) for cid,v in deltas.items()},
        cluster_bootstrap=dict(unit='case_id; retain all repeats of each sampled case',confidence=.95,
            seed=seed,samples=bootstrap_samples,low=percentile(samples,.025),high=percentile(samples,.975),
            lower_bound_is_acceptance_gate=False),
        improvement_10pp_met=gain>=.1-1e-12,
        goal_achieved=False,
        remaining_acceptance='This analysis alone cannot certify frozen candidate, unchanged environment, heldout independence, audit integrity or <=1.25 cost ratio.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pairs',required=True,help='JSON with case_ids and pairs [{reference:path,candidate:path}]')
    p.add_argument('--output',required=True)
    args=p.parse_args();spec=json.loads(Path(args.pairs).read_text());pairs=[];hashes={}
    for row in spec['pairs']:
        reports=[]
        for key in ('reference','candidate'):
            path=Path(row[key]);raw=path.read_bytes();hashes[str(path.resolve())]=hashlib.sha256(raw).hexdigest()
            reports.append(json.loads(raw))
        pairs.append(reports)
    result=analyze(pairs,spec['case_ids']);result['input_sha256']=hashes
    with Path(args.output).open('x') as stream:json.dump(result,stream,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k not in ('rounds','paired_case_gains','input_sha256')}))
