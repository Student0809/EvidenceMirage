"""Join sanitized provider receipts to local HTTP records without altering either."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.summarize_asr_call_journal import summarize


def canonical(row):
    keys=('created_at','type','model_name','quota','prompt_tokens','completion_tokens','use_time','group','billing')
    return json.dumps({k:row[k] for k in keys if k in row},sort_keys=True,separators=(',',':'))


def reconcile(summary, snapshots, pricing):
    counts=Counter();sources={};hashes={}
    for path in snapshots:
        path=Path(path).resolve();raw=path.read_bytes();snapshot=json.loads(raw)
        hashes[str(path)]=hashlib.sha256(raw).hexdigest()
        if snapshot.get('status_code')!=200 or snapshot.get('success') is not True:continue
        local=Counter(canonical(row) for row in snapshot['rows'])
        # Overlapping snapshots repeat receipts. Retain within-snapshot
        # multiplicity, but never add the same receipt once per polling cycle.
        for key,n in local.items():
            counts[key]=max(counts[key],n);sources.setdefault(key,[]).append(str(path))
    receipts=[(key,index,json.loads(key)) for key,n in counts.items() for index in range(n)]
    eligible={};receipt_claimants=Counter()
    for call in summary['records']:
        matches=[];usage=call.get('usage') or {}
        if (call['kind']=='target_http' and call['event']=='finish' and call['status']=='completed'
                and call.get('finished_at')):
            start=datetime.fromisoformat(call['started_at']).timestamp()
            end=datetime.fromisoformat(call['finished_at']).timestamp()
            for key,index,row in receipts:
                if (row.get('type')==2 and row.get('model_name')==call.get('model')
                        and row.get('prompt_tokens')==usage.get('prompt_tokens')
                        and row.get('completion_tokens')==usage.get('completion_tokens')
                        and start-2<=row.get('created_at',0)<=end+10):
                    matches.append((key,index,row));receipt_claimants[(key,index)]+=1
        eligible[call['call_id']]=matches
    used=set();records=[];scopes={}
    unit=pricing['target']['quota_per_unit'];fx=pricing['common_display_conversion']['rate']
    for call in summary['records']:
        value=dict(call);cost=None
        if call['kind']=='attacker_http':
            estimate=call.get('estimate_cny')
            cost=estimate['amount'] if estimate else None
            value['cost_basis']='published_DeepSeek_token_tariff_estimate'
        elif call['kind']=='target_http':
            matches=eligible[call['call_id']]
            value['receipt_matches']=len(matches)
            if len(matches)==1 and receipt_claimants[(matches[0][0],matches[0][1])]==1:
                key,index,row=matches[0];used.add((key,index))
                value.update(provider_quota=row['quota'],platform_usd_credit=row['quota']/unit,
                    provider_group=row['group'],receipt_sha256=hashlib.sha256(key.encode()).hexdigest(),
                    receipt_sources=sources[key])
                cost=row['quota']/unit*fx
            value['cost_basis']='matched_provider_quota; USD_platform_credit converted at frozen public display rate'
        value['comparable_cny_equivalent']=cost
        records.append(value)
        scope=value.get('attribution','unattributed')
        group=scopes.setdefault(scope,dict(http_attempts=0,known_cost_cny_equivalent=0,unknown_cost_requests=0))
        group['http_attempts']+=1
        if cost is None:group['unknown_cost_requests']+=1
        else:group['known_cost_cny_equivalent']+=cost
    unknown=sum(r['comparable_cny_equivalent'] is None for r in records)
    known=sum(r['comparable_cny_equivalent'] for r in records if r['comparable_cny_equivalent'] is not None)
    return dict(schema_version=1,http_attempts=len(records),unknown_cost_requests=unknown,
        total_comparable_cny_equivalent=known if not unknown and not summary['issues'] else None,
        known_subtotal_cny_equivalent=known,
        actual_target_quota_known=sum(r.get('provider_quota',0) for r in records),
        matched_target_requests=sum('provider_quota' in r for r in records),
        unmatched_snapshot_receipts=len(receipts)-len(used),records=records,by_case_or_preflight=scopes,
        source_summary_issues=summary['issues'],receipt_input_sha256=hashes,
        journal_input_sha256=summary['input_sha256'],
        semantics='Mixed cost basis: actual Target platform-credit debits plus published DeepSeek CNY token estimates, converted using frozen public display rate. Not an actual-cash invoice. Failed/incomplete/unmatched requests remain unknown; unmatched account receipts are not charged to this experiment. CLI logical usage is not added twice.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--journal',required=True,action='append');p.add_argument('--run',action='append',default=[])
    p.add_argument('--receipts',required=True,action='append');p.add_argument('--pricing',required=True)
    p.add_argument('--output',required=True);a=p.parse_args()
    pricing=json.loads(Path(a.pricing).read_text())
    paths=[]
    for name in a.receipts:
        path=Path(name);paths.extend(sorted(path.glob('*.json')) if path.is_dir() else [path])
    summary=summarize(a.journal,pricing,a.run);result=reconcile(summary,paths,pricing)
    result['pricing_sha256']=hashlib.sha256(Path(a.pricing).read_bytes()).hexdigest()
    with Path(a.output).open('x') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k not in ('records','receipt_input_sha256','journal_input_sha256','by_case_or_preflight')}))
