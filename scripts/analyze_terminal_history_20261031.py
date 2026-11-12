"""Offline comparison of the completed grounded-history run; no model calls."""
from collections import Counter, defaultdict
from pathlib import Path
import hashlib
import json
import math

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/a2_v2_terminal_history_test30_20261031_172750'
PREFIX = ROOT / 'reports/terminal_history_analysis_20261031'
HASHES = {}


def read(path, lines=False):
    path = Path(path)
    data = path.read_bytes()
    HASHES[str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)] = hashlib.sha256(data).hexdigest()
    return [json.loads(x) for x in data.splitlines() if x] if lines else json.loads(data)


def main():
    previous = read(ROOT / 'reports/terminal_revision_diagnosis_20261031.json')
    old_by_id = {c['case_id']: c for c in previous['cases']}
    reference = read(ROOT / 'data/piminer_terminal30_20261031/reference.json')
    paths, members, cases, generation_rows = defaultdict(Counter), defaultdict(set), [], []
    semantic, errors = Counter(), []
    for source in reference['sources']:
        cid = source['case_id']
        original = read(source['sample_path'])
        episodes = read(RUN / cid / 'episodes.jsonl', True)
        decisions = read(RUN / cid / 'decisions.jsonl', True)
        outcome, = read(RUN / cid / 'outcomes.json')
        gs = {}
        for file in sorted((RUN / cid / 'raw' / cid / 'stage_aware').glob('*.generation.json')):
            g = read(file)
            gs[file.name.split('.')[0]] = g
            branch = g['revision_path']
            members[branch].add(cid)
            paths[branch]['requests'] += 1
            paths[branch][g['status']] += 1
            paths[branch]['model_calls'] += len(g['calls'])
            row = dict(case_id=cid, generation_id=g['generation_id'], branch=branch,
                       status=g['status'], call_stages=dict(Counter(c['stage'] for c in g['calls'])))
            if branch == 'grounded_false':
                mem = g['revision_memory']
                prompt = json.loads(g['calls'][0]['prompt'].split('\nPUBLIC INPUT:\n', 1)[1])
                assert prompt['context']['strategy_document'].strip()
                assert len(mem['attempts']) == len(g['feedback_history_iters'])
                row.update(history_attempts=len(mem['attempts']),
                           positive_reference_candidates=mem['positive_reference_candidates'],
                           strategy_supplied=True,
                           cause=(g.get('plan') or {}).get('cause'),
                           format_corrections=g['planning']['format_corrections'],
                           planner_errors=[a['error'] for a in g['planning']['actions'] if 'error' in a],
                           semantic_checks=g['semantic_checks'])
                paths[branch]['strategy_supplied'] += 1
                paths[branch]['history_gt_one'] += len(mem['attempts']) > 1
                paths[branch]['positive_history_generations'] += bool(mem['positive_reference_candidates'])
                paths[branch]['format_corrections'] += bool(row['format_corrections'])
                paths[branch]['successful_format_corrections'] += bool(row['format_corrections']) and g['status'] == 'ready'
                if g.get('plan'):
                    assert g['plan']['cause'] == 'unknown'
                reviews = g['semantic_checks']
                for s in reviews:
                    semantic['reviews_' + s['status']] += 1
                    for k, v in s.get('assessments', {}).items():
                        semantic[k + '_' + v] += 1
                if reviews:
                    first, last = reviews[0], reviews[-1]
                    for label, s in [('first', first), ('last', last)]:
                        semantic[label + '_conflicting_generations'] += 'conflict' in s.get('assessments', {}).values()
                    if len(reviews) > 1:
                        semantic['generations_semantically_repaired'] += 1
                        for dimension in ['evidence_alignment', 'plan_following']:
                            if first.get('assessments', {}).get(dimension) == 'conflict':
                                semantic[dimension + '_initial_generations'] += 1
                                semantic[dimension + '_no_longer_flagged'] += last.get('assessments', {}).get(dimension) != 'conflict'
                if g['status'] != 'ready':
                    errors.append(dict(case_id=cid, generation_id=g['generation_id'],
                                       planner_errors=row['planner_errors'], unused_target_slots=5-len(episodes)))
            generation_rows.append(row)
        attempts = []
        for index, e in enumerate(episodes):
            raw = read(RUN / cid / 'raw' / (e['episode_id'] + '.json'))
            assert hashlib.sha256(raw['payload'].encode()).hexdigest() == e['payload_sha256']
            if index == 0:
                assert raw['payload'] == original['history'][-1]['injection']
                assert e['payload_sha256'] == old_by_id[cid]['attempts'][0]['payload_sha256']
            origin = gs[e['payload_id']]['revision_path'] if e['payload_id'] in gs else 'imported_terminal'
            action = 'Initial' if index == 0 else decisions[index-1]['action']
            paths[origin]['executions'] += 1
            paths[origin]['trigger_' + e['trigger']] += 1
            paths[origin]['gold_true'] += e['gold'] is True
            paths[origin]['first_candidate_executions'] += action == 'Revise'
            paths[origin]['first_candidate_trigger_T'] += action == 'Revise' and e['trigger'] == 'T'
            tools = []
            for m in raw['result']['raw'].get('messages', []):
                for call in m.get('tool_calls') or []:
                    f = call['function']
                    tools.append(f if isinstance(f, str) else f['name'])
            attempts.append(dict(episode_id=e['episode_id'], payload_id=e['payload_id'],
                                 origin=origin, action=action, trigger=e['trigger'], gold=e['gold'],
                                 payload_sha256=e['payload_sha256'], tool_names=tools))
        success = any(e['gold'] is True for e in episodes)
        first = next((a for a in attempts if a['gold'] is True), None)
        cases.append(dict(case_id=cid, historical_success=any(h['security'] is True for h in original['history']),
                          previous_initial=old_by_id[cid]['initial_gold'], previous_success=old_by_id[cid]['new_success'],
                          initial=episodes[0]['gold'], success=success, status=outcome['status'],
                          first_success_origin=first['origin'] if first else None, attempts=attempts))
    for branch, ids in members.items():
        paths[branch]['cases'] = len(ids)
        paths[branch]['successful_candidate_cases'] = sum(any(a['origin']==branch and a['gold'] is True for a in c['attempts']) for c in cases)
    gained = [c['case_id'] for c in cases if c['success'] and not c['previous_success']]
    lost = [c['case_id'] for c in cases if c['previous_success'] and not c['success']]
    n = len(gained)+len(lost)
    mcnemar = min(1.0, 2 * sum(math.comb(n, i) for i in range(min(len(gained),len(lost))+1)) / 2**n)
    confirmed = sum(c['success'] for c in cases)
    summary = dict(cases=len(cases), initial_successes=sum(c['initial'] is True for c in cases),
                   confirmed_successes=confirmed,
                   generation_errors=len(errors), executions=sum(len(c['attempts']) for c in cases),
                   actions=dict(Counter(a['action'] for c in cases for a in c['attempts'])),
                   gained_vs_previous=gained, lost_vs_previous=lost,
                   historical_hits_not_reproduced_initially=[c['case_id'] for c in cases if c['historical_success'] and not c['initial']],
                   historical_hits_not_recovered=[c['case_id'] for c in cases if c['historical_success'] and not c['success']],
                   historical_misses_recovered=[c['case_id'] for c in cases if not c['historical_success'] and c['success']],
                   unused_slots_after_errors=sum(e['unused_target_slots'] for e in errors),
                   observed_confirmed_fraction=confirmed/len(cases),
                   unresolved_case_bounds=[confirmed/len(cases),(confirmed+len(errors))/len(cases)],
                   paired_exact_mcnemar_two_sided=mcnemar,
                   all_p0_equal_to_historical_terminal_and_previous_run=True)
    assert summary['confirmed_successes']==8 and summary['initial_successes']==6
    assert summary['executions']==114 and len(generation_rows)==83
    data = dict(api_calls=0, run=str(RUN), summary=summary,
                revision_paths={k:dict(v) for k,v in paths.items()}, semantic=dict(semantic),
                errors=errors, cases=cases, generations=generation_rows, source_sha256=HASHES,
                limitations=['Fresh H1, no fixed-feedback causal control; one run per implementation.',
                             'Two generation-error cases have unresolved final outcomes.',
                             'Semantic reviews are advisory model judgments, not ground truth.',
                             'Multiple candidate executions within one case are not independent samples.'])
    PREFIX.with_suffix('.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    table = ['| 案例 | 原历史成功 | 上轮首轮/最终 | 本轮首轮/最终 | 本轮逐次 Trigger（+为 Gold 成功） | 终态 |',
             '|---|---|---|---|---|---|']
    mark = lambda b: '✓' if b else '—'
    for c in cases:
        seq = ' → '.join(a['payload_id']+':'+a['trigger']+('+' if a['gold'] else '') for a in c['attempts'])
        table.append(f"| {c['case_id']} | {mark(c['historical_success'])} | {mark(c['previous_initial'])}/{mark(c['previous_success'])} | {mark(c['initial'])}/{mark(c['success'])} | {seq} | {c['status']} |")
    PREFIX.with_name(PREFIX.name+'_table.md').write_text('\n'.join(table)+'\n')
    print(json.dumps(dict(summary=summary,revision_paths=data['revision_paths'],semantic=data['semantic']),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
