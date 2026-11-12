"""Aggregate disjoint receipt reconciliations and allocate shared P0 once.

All preflight/failure HTTP rows remain in each run's reconciliation. Incomplete
runs and unknown amounts prevent a complete total or passing cost comparison.
This is platform-credit/tariff accounting, not a cash purchase invoice.
"""
import argparse
import hashlib
import json
from pathlib import Path


def aggregate(entries):
    seen = set()
    phases = {}
    rows = []
    for entry in entries:
        phase = phases.setdefault(entry['phase'], {})
        role = entry['role']
        if role not in ('reference', 'candidate', 'shared_initialization'):
            raise ValueError('Unknown accounting role')
        report = entry.get('report')
        if report is not None:
            ids = [r['call_id'] for r in report['records']]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise ValueError('Overlapping reconciliations would double count HTTP calls')
            seen.update(ids)
            cost = report['total_comparable_cny_equivalent']
            known = report['known_subtotal_cny_equivalent']
            unknown = report['unknown_cost_requests']
            count = report['http_attempts']
            if count != len(ids):
                raise ValueError('HTTP denominator differs from records')
        else:
            cost, known, unknown, count = None, 0, None, None
        complete = entry['completed'] is True and cost is not None
        row = dict(id=entry['id'], phase=entry['phase'], role=role,
                   completed=entry['completed'], http_attempts=count,
                   unknown_cost_requests=unknown, known_subtotal_cny_equivalent=known,
                   complete_cost_cny_equivalent=cost if complete else None,
                   assigned_case_repeats=entry.get('assigned_case_repeats', 0))
        rows.append(row)
        phase.setdefault(role, []).append(row)
    comparisons = {}
    for name, groups in phases.items():
        totals = {}
        shared = groups.get('shared_initialization', [])
        shared_complete = all(r['complete_cost_cny_equivalent'] is not None for r in shared)
        shared_total = sum(r['complete_cost_cny_equivalent'] or 0 for r in shared)
        for role in ('reference', 'candidate'):
            selected = groups.get(role, [])
            denominator = sum(r['assigned_case_repeats'] for r in selected)
            complete = (bool(selected) and denominator > 0 and shared_complete
                        and all(r['complete_cost_cny_equivalent'] is not None for r in selected))
            total = (sum(r['complete_cost_cny_equivalent'] for r in selected)
                     + shared_total / 2) if complete else None
            totals[role] = dict(assigned_case_repeats=denominator, total_cny_equivalent=total,
                               per_case_cny_equivalent=total/denominator if complete else None)
        reference, candidate = totals['reference'], totals['candidate']
        ratio = None
        if (reference['assigned_case_repeats'] == candidate['assigned_case_repeats']
                and reference['per_case_cny_equivalent'] is not None
                and reference['per_case_cny_equivalent'] > 0
                and candidate['per_case_cny_equivalent'] is not None):
            ratio = candidate['per_case_cny_equivalent'] / reference['per_case_cny_equivalent']
        comparisons[name] = dict(**totals, cost_ratio=ratio,
            cost_ratio_1_25_met=None if ratio is None else ratio <= 1.25,
            shared_initialization_allocation='One half per arm, charged once across all repeats')
    complete = bool(rows) and all(r['complete_cost_cny_equivalent'] is not None for r in rows)
    return dict(schema_version=1, entries=rows, phases=comparisons,
                known_subtotal_cny_equivalent=sum(r['known_subtotal_cny_equivalent'] for r in rows),
                total_cny_equivalent=sum(r['complete_cost_cny_equivalent'] for r in rows) if complete else None,
                recorded_http_attempts=len(seen), complete=complete, goal_achieved=False,
                semantics='All declared runs, including diagnostic/preflight/failed requests. Shared initialization charged once, not once per repeat. Unknown costs or unfinished runs keep total null. No claim of actual cash purchase cost.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    registry_path = Path(args.registry).resolve()
    registry = json.loads(registry_path.read_text())
    hashes = {str(registry_path): hashlib.sha256(registry_path.read_bytes()).hexdigest()}
    entries = []
    for source in registry['entries']:
        entry = dict(source)
        report_path = entry.get('report')
        if report_path:
            path = Path(report_path).resolve()
            hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            entry['report'] = json.loads(path.read_text())
        state = Path(entry.pop('run')) / 'status.json'
        entry['completed'] = False
        if state.exists():
            blob = state.read_bytes()
            hashes[str(state.resolve())] = hashlib.sha256(blob).hexdigest()
            entry['completed'] = json.loads(blob).get('status') in ('completed', 'completed_with_errors')
        entries.append(entry)
    result = aggregate(entries)
    result.update(scope=registry['scope'], input_sha256=hashes)
    with Path(args.output).open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k not in ('entries', 'input_sha256')}))


if __name__ == '__main__':
    main()
