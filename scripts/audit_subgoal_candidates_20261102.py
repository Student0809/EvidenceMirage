"""Offline descriptive audit; privileged traces never enter attacker feedback."""
import collections
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/subgoal_evidence_sampling_20261102'
RUNS = {
    'history_v1': 'a2_v2_terminal_history_test30_20261031_172750',
    'plan_memory_v2': 'a2_v2_plan_memory_test30_20261031_224926',
}


def main():
    hashes = {}

    def read(path):
        blob = path.read_bytes()
        hashes[str(path.relative_to(ROOT))] = hashlib.sha256(blob).hexdigest()
        return blob.decode()

    result = {'scope': 'offline descriptive audit, not online observable evidence',
              'new_target_calls': 0, 'runs': {}}
    for name, directory in RUNS.items():
        base = ROOT / 'runs' / directory
        rows = []
        for path in sorted(base.glob('c*/episodes.jsonl')):
            for line in read(path).splitlines():
                e = json.loads(line)
                d = json.loads(read(path.parent / e['raw_path']))['result']
                raw = d.get('raw') or {}
                calls = [c for m in raw.get('messages', [])
                         for c in (m.get('tool_calls') or [])]
                # Includes supplied prefix. A call here is NOT automatically
                # a new post-injection action or an attacker-induced action.
                functions = [c['function'] for c in calls]
                obs = e.get('observation') or {}
                rows.append(dict(case_id=e['case_id'], episode_id=e['episode_id'],
                    payload_id=e['payload_id'], trigger=e['trigger'], gold=e['gold'],
                    predicates=[{'id': p['id'], 'status': p['status']}
                                for p in e.get('evidence', [])],
                    authorized_query_ids=obs.get('event_coverage', {}).get('query_ids', []),
                    full_trace_tool_names=functions,
                    raw_path=str((path.parent / e['raw_path']).relative_to(ROOT))))
        result['runs'][name] = dict(directory=directory, executions=len(rows),
            triggers=dict(collections.Counter(r['trigger'] for r in rows)),
            trigger_gold=dict(collections.Counter(f"{r['trigger']}/{r['gold']}" for r in rows)),
            unknown_with_empty_predicates=sum(r['trigger']=='U' and not r['predicates'] for r in rows),
            unknown_without_coverage_queries=sum(r['trigger']=='U' and not r['authorized_query_ids'] for r in rows),
            rows=rows)
    library = json.loads(read(ROOT / 'strategy_library_a2/library.json'))
    result['library'] = dict(strategies=len(library['strategies']), examples=len(library['cases']),
        datasets=dict(collections.Counter(c['dataset'] for c in library['cases'])),
        outcome_labels=dict(collections.Counter(c['outcome_label'] for c in library['cases'])))
    assert result['runs']['history_v1']['triggers'] == {'F':71, 'U':36, 'T':7}
    hashes[str(Path(__file__).relative_to(ROOT))] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result['input_sha256'] = hashes
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'offline_audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:{a:b for a,b in v.items() if a!='rows'} for k,v in result['runs'].items()}, indent=2))


if __name__ == '__main__':
    main()
