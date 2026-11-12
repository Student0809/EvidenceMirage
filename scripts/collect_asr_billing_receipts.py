"""Read-only Target billing receipts for this screen; no inference or resampling."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid
import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from run_observable_pilot5_20261029 import configure_credentials


def collect(since):
    record=dict(kind='target_billing_receipts',paid_model_calls=0,api_requests=1,
        observed_at=datetime.now(timezone.utc).isoformat(),source_endpoint='/api/log/token',
        authentication='Bearer; credential omitted',rows=[])
    try:
        response=httpx.get(os.environ['OPENAI_BASE_URL'].removesuffix('/v1')+'/api/log/token',
            headers={'Authorization':'Bearer '+os.environ['OPENAI_API_KEY']},timeout=30,follow_redirects=False)
        record['status_code']=response.status_code
        value=response.json();record['success']=value.get('success') is True
        rows=value.get('data')
        if not isinstance(rows,list):raise ValueError('Billing rows unavailable')
        record['returned_rows']=len(rows)
        record['oldest_returned_at']=min((r.get('created_at',0) for r in rows),default=None)
        for row in rows:
            if row.get('created_at',0)<since or row.get('model_name')!='gpt-5-nano':continue
            item={k:row[k] for k in ('created_at','type','model_name','quota','prompt_tokens',
                'completion_tokens','use_time','group') if k in row}
            other=row.get('other',{})
            if isinstance(other,str):other=json.loads(other)
            item['billing']={k:other[k] for k in ('cache_ratio','cache_tokens','completion_ratio',
                'group_ratio','model_ratio','model_price','user_group_ratio') if k in other}
            record['rows'].append(item)
    except Exception as exc:
        record['error_type']=type(exc).__name__
    return record


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',required=True)
    p.add_argument('--output',required=True)
    p.add_argument('--once',action='store_true')
    args=p.parse_args();run=Path(args.run).resolve();output=Path(args.output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    status=json.loads((run/'status.json').read_text())
    since=int(datetime.fromisoformat(status['started_at']).timestamp())
    configure_credentials();failures=0
    while True:
        status=json.loads((run/'status.json').read_text())
        terminal=status['status'] in ('awaiting_analysis','stopped_402','interrupted')
        if terminal:time.sleep(5)
        result=collect(since)
        path=output/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]+'.json')
        with path.open('x') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
        print(json.dumps(dict(file=str(path),status_code=result.get('status_code'),
            rows=len(result['rows']),error_type=result.get('error_type'),paid_model_calls=0)),flush=True)
        failures=failures+1 if result.get('error_type') or result.get('success') is not True else 0
        if args.once or terminal or failures>=3:return
        time.sleep(300)


if __name__=='__main__':main()
