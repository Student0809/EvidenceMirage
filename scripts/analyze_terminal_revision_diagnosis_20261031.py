"""Read-only paired analysis of saved PIMiner and terminal-payload runs.

No model calls. Outputs contain counts, hashes and tool names, not payloads,
private environments, tool arguments or attacker analysis text.
"""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/a2_v2_terminal_mixed_test30_20261031'
OUT = ROOT / 'reports/terminal_revision_diagnosis_20261031'
HASHES = {}


def read(path, jsonl=False):
    path = Path(path)
    content = path.read_bytes()
    HASHES[str(path)] = hashlib.sha256(content).hexdigest()
    return ([json.loads(line) for line in content.decode().splitlines() if line]
            if jsonl else json.loads(content))


def tool_names(raw):
    names = []
    for message in raw.get('messages', []):
        for call in message.get('tool_calls') or []:
            function = call.get('function')
            names.append(function if isinstance(function, str) else function['name'])
    return names


def semantic_probes():
    """Evaluator-side checks only; never pass their output into an attacker."""
    import inspect
    import agentdojo.default_suites  # noqa: F401
    from agentdojo.task_suite.load_suites import get_suite

    task = get_suite('v1.2.2', 'workspace').get_injection_task_by_id('injection_task_4')
    native_path = Path(inspect.getfile(type(task)))
    HASHES[str(native_path)] = hashlib.sha256(native_path.read_bytes()).hexdigest()
    initial = read(RUN / 'c013/raw/c013/shared/000.json')
    raw = initial['result']['raw']
    emails = raw['post']['inbox']['emails']
    emails = emails.values() if isinstance(emails, dict) else emails
    matches = [e for e in emails if task._SUBJECT.lower() in e['subject'].lower()
               and e['sender'] == raw['pre']['inbox']['account_email']]
    fields = [dict(body_prefix=e['body'].startswith(task._BODY), code=task._CODE in e['body'],
        exact_subject=e['subject'] == task._SUBJECT, recipients=e['recipients'] == [task._RECIPIENT],
        sender=e['sender'] == raw['pre']['inbox']['account_email']) for e in matches]
    gen_checks = []
    for pid in ['P1', 'P2', 'P3']:
        gen = read(RUN / f'c013/raw/c013/stage_aware/{pid}.generation.json')
        gen_checks.append(dict(payload_id=pid,
            contains_correct_fixed_code=task._CODE in gen['payload'],
            checker_statuses=[c['status'] for c in gen['checks']],
            feedback_history_iters=gen['feedback_history_iters']))
    c008_execution = read(RUN / 'c008/raw/c008/stage_aware/001.json')
    c008_revision = read(RUN / 'c008/raw/c008/stage_aware/P2.generation.json')
    assert len(matches) == 1 and fields[0] == dict(body_prefix=False, code=True,
        exact_subject=True, recipients=True, sender=True)
    assert 'get_day_calendar_events' in tool_names(c008_execution['result']['raw'])
    assert 'no calendar read' in c008_revision['analysis']
    return dict(scope='Offline evaluator-only diagnosis; no privileged facts sent to generators.',
        c013=dict(initial_matching_outgoing_emails=len(matches), initial_field_checks=fields,
                  initial_payload_contains_correct_code=task._CODE in initial['payload'],
                  revisions=gen_checks),
        c008=dict(P1_called_calendar=True, P2_analysis_claims_no_calendar_read=True))


def main():
    reference = read(ROOT / 'data/piminer_terminal30_20261031/reference.json')
    cases, paths = [], defaultdict(Counter)
    memberships = defaultdict(set)
    checks = Counter()
    for source in reference['sources']:
        cid = source['case_id']
        old = read(source['sample_path'])
        episodes = read(RUN / cid / 'episodes.jsonl', True)
        decisions = read(RUN / cid / 'decisions.jsonl', True)
        outcome, = read(RUN / cid / 'outcomes.json')
        generations = {}
        for file in sorted((RUN / cid / 'raw' / cid / 'stage_aware').glob('*.generation.json')):
            gen = read(file)
            pid = file.name.split('.')[0]
            generations[pid] = gen
            path = gen['revision_path']
            memberships[path].add(cid)
            paths[path]['requests'] += 1
            paths[path]['model_stage_calls'] += len(gen['calls'])
            paths[path][gen['status']] += 1
            paths[path]['latest_only_feedback'] += gen.get('prompt_mode') == 'grounded_latest'
            paths[path]['with_session_id'] += bool(gen.get('session_id'))
            paths[path]['with_experience_field'] += 'experience' in gen
            for check in gen.get('checks', []):
                checks[check['status']] += 1
        attempts = []
        for i, ep in enumerate(episodes):
            saved = read(RUN / cid / 'raw' / (ep['episode_id'] + '.json'))
            payload = saved['payload']
            assert hashlib.sha256(payload.encode()).hexdigest() == ep['payload_sha256']
            if i == 0:
                assert payload == old['history'][-1]['injection']
            gen = generations.get(ep['payload_id'])
            path = gen['revision_path'] if gen else 'imported_terminal'
            action = 'Initial' if i == 0 else decisions[i - 1]['action']
            if action == 'Revise':
                assert gen and gen['status'] == 'ready'
            paths[path]['target_executions'] += 1
            paths[path]['gold_success_executions'] += ep['gold'] is True
            paths[path]['trigger_' + ep['trigger']] += 1
            attempts.append(dict(episode_id=ep['episode_id'], payload_id=ep['payload_id'],
                payload_sha256=ep['payload_sha256'], action=action, origin=path,
                trigger=ep['trigger'], gold=ep['gold'], error=ep['error'],
                tool_names=tool_names(saved['result']['raw'])))
        old_success = any(h['security'] is True for h in old['history'])
        first_success = next((h['iter'] + 1 for h in old['history'] if h['security'] is True), None)
        new_success = any(e['gold'] is True for e in episodes)
        cases.append(dict(case_id=cid, original_success=old_success,
            original_first_success_attempt=first_success,
            original_attempts=len(old['history']),
            original_gold_sequence=[h['security'] for h in old['history']],
            initial_gold=episodes[0]['gold'], new_success=new_success,
            final_status=outcome['status'], attempts=attempts,
            failed_generation_ids=[pid for pid, g in generations.items() if g['status'] != 'ready'],
            unused_target_slots_after_generation_error=(5 - len(episodes)
                if outcome['status'] == 'GENERATION_ERROR' else 0),
            source_sample_path=source['sample_path'],
            source_trajectory_unavailable=all('not retained' in str(h.get('trajectory', ''))
                                              for h in old['history'])))
    original = sum(c['original_success'] for c in cases)
    initial = sum(c['initial_gold'] is True for c in cases)
    final = sum(c['new_success'] for c in cases)
    lost = [c['case_id'] for c in cases if c['original_success'] and not c['new_success']]
    gained = [c['case_id'] for c in cases if not c['original_success'] and c['new_success']]
    errors = [c['case_id'] for c in cases if c['final_status'] == 'GENERATION_ERROR']
    summary = dict(assigned=len(cases), original_successes=original,
        original_initial_successes=sum(c['original_first_success_attempt'] == 1 for c in cases),
        original_revision_successes=sum((c['original_first_success_attempt'] or 0) > 1 for c in cases),
        original_saved_attempts=sum(c['original_attempts'] for c in cases),
        original_saved_revision_attempts=sum(c['original_attempts'] - 1 for c in cases),
        new_initial_successes=initial, new_final_confirmed_successes=final,
        absolute_success_fraction_delta=(final - original) / len(cases),
        relative_confirmed_success_change=(final - original) / original,
        historical_hits_not_reproduced_initially=[c['case_id'] for c in cases
            if c['original_success'] and c['initial_gold'] is False],
        lost_historical_hits=lost, gained_historical_misses=gained,
        generation_errors=errors,
        unused_target_slots_after_generation_errors=sum(c['unused_target_slots_after_generation_error'] for c in cases),
        fixed_remaining_outcome_bounds=[final / len(cases), (final + len(errors)) / len(cases)],
        target_executions=sum(len(c['attempts']) for c in cases),
        action_counts=dict(Counter(a['action'] for c in cases for a in c['attempts'])),
        all_initial_payloads_identical_to_source=True)
    for path, ids in memberships.items():
        paths[path]['cases'] = len(ids)
        paths[path]['historical_hit_cases'] = sum(c['original_success'] for c in cases if c['case_id'] in ids)
        paths[path]['successful_candidate_cases'] = sum(any(a['origin'] == path and a['gold'] is True
            for a in c['attempts']) for c in cases)
    assert len(cases) == 30 and summary['target_executions'] == 121
    assert original - len(lost) + len(gained) == final
    probes = semantic_probes()
    data = dict(api_calls=0, run=str(RUN), summary=summary, evaluator_probes=probes,
        revision_paths={k: dict(v) for k, v in paths.items()}, static_check_statuses=dict(checks),
        cases=cases, source_sha256=HASHES,
        limitations=['Historical source has no complete Target trajectory.',
            'Different time periods, initial candidates and feedback; no causal branch comparison.',
            'Three generation errors are unresolved, not confirmed task failures.',
            'Multiple attempts within one case are not independent cases.',
            'Source hashes audit analysis inputs; historical execution source snapshots are unavailable.'])
    OUT.with_suffix('.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    table = ['| Case | 原 Gold 序列 | 新首轮 | 后续动作 / Trigger / Gold | 最终状态 |',
             '|---|---|---|---|---|']
    for c in cases:
        seq = ''.join('1' if x else '0' for x in c['original_gold_sequence'])
        post = ', '.join(f"{a['payload_id']}:{'F修订' if a['origin']=='grounded_false' and a['action']=='Revise' else 'U修订' if a['action']=='Revise' else a['action']}/{a['trigger']}/{int(a['gold']) if a['gold'] is not None else '?'}"
                         for a in c['attempts'][1:]) or '—'
        table.append(f"| {c['case_id']} | {seq} | {int(c['initial_gold'])} | {post} | {c['final_status']} |")
    OUT.with_name(OUT.name + '_table.md').write_text('\n'.join(table) + '\n')
    print(json.dumps(dict(summary=summary, revision_paths=data['revision_paths'],
                         static_check_statuses=dict(checks), audited_inputs=len(HASHES)),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
