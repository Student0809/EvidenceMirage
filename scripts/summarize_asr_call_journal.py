"""Offline accounting of actual HTTP attempts, including preflights and failures.

CLI records are logical invocations, never added to HTTP counts or token totals.
Amounts use frozen provider tariffs; unavailable usage/pricing stays unknown.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo


def number(value):
    return type(value) in (int, float) and value >= 0


def estimate(record, pricing):
    usage = record.get('usage')
    if (record.get('kind') != 'attacker_http' or record.get('event') != 'finish'
            or record.get('usage_complete') is not True or not isinstance(usage, dict)):
        return None
    names = ('input_tokens', 'output_tokens', 'cache_read_input_tokens')
    if not all(number(usage.get(name)) for name in names):
        return None
    # DeepSeek Anthropic input_tokens excludes cache_read_input_tokens. A new
    # nonzero cache-creation field needs an independently verified tariff.
    if usage.get('cache_creation_input_tokens', 0) != 0:
        return None
    server = usage.get('server_tool_use', {})
    if not isinstance(server, dict) or any(v != 0 for v in server.values()):
        return None  # Search/fetch billing needs success counters, not attempts.
    rates = pricing['attacker']
    def amount(tier):
        r = rates[tier]
        return (usage['input_tokens'] * r['cache_miss']
                + usage['cache_read_input_tokens'] * r['cache_hit']
                + usage['output_tokens'] * r['output']) / 1_000_000
    start = datetime.fromisoformat(record['started_at']).astimezone(ZoneInfo('Asia/Shanghai'))
    end = datetime.fromisoformat(record['finished_at']).astimezone(ZoneInfo('Asia/Shanghai'))
    # Weekend calls that remain on the same date are unambiguously off-peak.
    # Otherwise keep a tariff range rather than guessing holiday/billing time.
    low, high = amount('offpeak'), amount('peak')
    exact = start.date() == end.date() and start.weekday() >= 5
    return dict(currency='CNY', estimated_not_invoice=True,
                amount=low if exact else None, minimum=low, maximum=low if exact else high,
                tier='offpeak_weekend' if exact else 'time_or_holiday_unresolved')


def attribution(runs):
    owners, hashes = {}, {}
    def scan(value, owner):
        if isinstance(value, dict):
            cid = value.get('call_id')
            if isinstance(cid,str):
                if cid in owners and owners[cid] != owner:
                    raise ValueError('One call attributed to multiple cases')
                owners[cid] = owner
            for item in value.values():scan(item,owner)
        elif isinstance(value,list):
            for item in value:scan(item,owner)
    for run in runs:
        run=Path(run).resolve()
        paths=[(run/'preflight.json',str(run)+'/preflight')]
        paths.extend((p,str(run)+'/'+p.relative_to(run).parts[0]) for p in run.glob('c*/raw/**/*.json'))
        for path,owner in paths:
            if path.exists():
                raw=path.read_bytes();hashes[str(path)]=hashlib.sha256(raw).hexdigest()
                scan(json.loads(raw),owner)
    return owners, hashes


def summarize(paths, pricing, runs=()):
    latest, hashes, issues = {}, {}, []
    for path in paths:
        path = Path(path).resolve()
        raw = path.read_bytes()
        hashes[str(path)] = hashlib.sha256(raw).hexdigest()
        for index, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                cid = row['call_id']
                if not isinstance(cid, str) or not cid:
                    raise ValueError('missing call_id')
            except (ValueError, KeyError, TypeError):
                issues.append(dict(path=str(path), line=index, code='invalid_journal_record'))
                continue
            key = (str(path), cid)
            previous = latest.get(key)
            if previous and previous.get('event') == 'finish' and row != previous:
                issues.append(dict(path=str(path), call_id=cid, code='event_after_finish'))
            latest[key] = row
    calls = list(latest.values())
    owners, attribution_hashes = attribution(runs)
    groups = {}
    for kind in ('target_http', 'attacker_http', 'attacker_cli'):
        rows = [r for r in calls if r['kind'] == kind]
        dispatched = [r for r in rows if r.get('dispatched') is True]
        usage = [r for r in dispatched if isinstance(r.get('usage'), dict)]
        groups[kind] = dict(records=len(rows), dispatched=len(dispatched),
            statuses=dict(Counter(r.get('status') for r in dispatched)),
            stages=dict(Counter(r.get('stage') for r in dispatched)),
            status_codes=dict(Counter(str(r['status_code']) for r in dispatched if r.get('status_code') is not None)),
            unfinished=sum(r.get('event') != 'finish' for r in dispatched),
            usage_present=len(usage), usage_missing=len(dispatched)-len(usage),
            partial_stream_usage=sum(r.get('usage_complete') is False and isinstance(r.get('usage'),dict) for r in dispatched),
            tokens_observed={name:sum(r['usage'].get(name, 0) for r in usage if number(r['usage'].get(name)))
                for name in ('input_tokens','output_tokens','cache_read_input_tokens','prompt_tokens','completion_tokens')})
    http = [r for r in calls if r.get('kind') in ('attacker_http','target_http') and r.get('dispatched') is True]
    estimates = [(r, estimate(r, pricing)) for r in http]
    known = [e for _, e in estimates if e is not None]
    all_exact = (not issues and len(known) == len(http)
                 and all(e['amount'] is not None for e in known))
    scopes = {}
    for row, value in estimates:
        scope=owners.get(row['call_id'],owners.get(row.get('parent_call_id'),'unattributed'))
        item=scopes.setdefault(scope,dict(http_attempts=0,unknown_amount_requests=0,
            known_subtotal_minimum_cny=0,known_subtotal_maximum_cny=0))
        item['http_attempts']+=1
        if value is None:item['unknown_amount_requests']+=1
        else:
            item['known_subtotal_minimum_cny']+=value['minimum']
            item['known_subtotal_maximum_cny']+=value['maximum']
    return dict(schema_version=1, api_calls_made_by_this_script=0,
        http_attempts=len(http), logical_attacker_invocations=groups['attacker_cli']['dispatched'],
        groups=groups, issues=issues, input_sha256=hashes,
        attribution_input_sha256=attribution_hashes,by_case_or_preflight=scopes,
        monetary=dict(currency='CNY', estimated_not_provider_invoice=True,
            complete=all_exact, total=sum(e['amount'] for e in known) if all_exact else None,
            estimated_requests=len(known), unknown_amount_requests=len(http)-len(known),
            known_subtotal_minimum=sum(e['minimum'] for e in known),
            known_subtotal_maximum=sum(e['maximum'] for e in known),
            target_tariff_status=pricing['target']['status']),
        records=[dict(call_id=r['call_id'],parent_call_id=r.get('parent_call_id'),run_id=r.get('run_id'),
            attribution=owners.get(r['call_id'],owners.get(r.get('parent_call_id'),'unattributed')),
            kind=r['kind'],model=r.get('model'),stage=r.get('stage'),event=r['event'],status=r.get('status'),
            started_at=r.get('started_at'),finished_at=r.get('finished_at'),
            http_requests=r.get('http_requests'),usage=r.get('usage'),usage_complete=r.get('usage_complete'),
            estimate_cny=e) for r,e in estimates],
        semantics='All dispatched HTTP attempts including preflights, retries, failures. CLI usage excluded from sums to avoid double counting. Unknown cost is not zero. HTTP attempts are not Target executions; tool loops can make multiple HTTP calls.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--journal', required=True, action='append')
    p.add_argument('--pricing', required=True)
    p.add_argument('--run', action='append', default=[])
    p.add_argument('--output', required=True)
    args = p.parse_args()
    result = summarize(args.journal, json.loads(Path(args.pricing).read_text()), args.run)
    result['pricing_sha256'] = hashlib.sha256(Path(args.pricing).read_bytes()).hexdigest()
    with Path(args.output).open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k:v for k,v in result.items() if k not in ('records','input_sha256','attribution_input_sha256')}))
