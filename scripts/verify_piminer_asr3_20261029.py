"""Offline provenance, policy, prompt and native-score replay for ASR@3 baseline."""
import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_piminer_asr3_20261029 import CASE_IDS, load_initial
from scripts.verify_observable_pilot5_20261029 import (
    VerificationError, read, read_lines, require, verify_episode)
from src.attacker.piminer import PUBLIC_FIELDS
from src.engine.budget import Budget
from src.engine.config import data_fingerprints, check_frozen, digest
from src.engine.history import History
from src.engine.storage import write_json
from src.policies.always_revise import AlwaysRevise
from src.schema import Episode, payload_hash
from src.vendor.prompts import build_user_message_blocks


def expected_prompt(case, route_document, attempts):
    public = {key: deepcopy(case[key]) for key in PUBLIC_FIELDS}
    visible = [dict(row, iter=i) for i, row in enumerate(attempts)]
    return '\n'.join(build_user_message_blocks(route_document, public, visible, threat_model='black_box'))


def verify_case(directory, case, manifest):
    config, cid = manifest['config'], case['case_id']
    source = Path(manifest['source_run'])
    initial = load_initial(source, case)
    child = read(directory / 'manifest.json')
    require(child['cases'] == [case] and child['case_ids'] == [cid], 'case_snapshot_mismatch')
    require(child['initial_provenance'] == initial['provenance'], 'initial_provenance_mismatch')
    require(child['source_run'] == str(source) and child['reused_initial'] is True, 'initial_reuse_not_declared')
    for key in ('config', 'environment'):
        require(child[key] == manifest[key], 'child_' + key + '_mismatch')
    require(data_fingerprints(child['inputs_sha256']) == data_fingerprints(manifest['inputs_sha256']),
            'child_inputs_sha256_mismatch')
    require(read(directory / 'status.json')['status'] == 'completed', 'case_not_completed')
    episodes, events, decisions = [read_lines(directory / (name + '.jsonl')) for name in ('episodes', 'events', 'decisions')]
    require(episodes and episodes[0] == initial['episode'], 'initial_episode_not_exact_copy')
    h1, branch = episodes[0], episodes[1:]
    require((directory / h1['raw_path']).read_bytes() == initial['raw_path'].read_bytes(), 'initial_raw_not_exact_copy')
    copied_generation = directory / ('raw/' + initial['generation']['generation_id'] + '.generation.json')
    require(copied_generation.read_bytes() == initial['generation_path'].read_bytes(), 'initial_generation_not_exact_copy')
    require(len(branch) <= 2, 'more_than_three_total_executions')
    expected_ids = [f'{cid}/always_revise/{i:03d}' for i in range(1, len(branch)+1)]
    require([e['episode_id'] for e in branch] == expected_ids, 'branch_episode_sequence_mismatch')
    require(all(e['policy_id'] == 'always_revise' and e['phase'] == 'post_failure' for e in branch), 'branch_identity_mismatch')
    require(len({e['episode_id'] for e in episodes}) == len(episodes), 'duplicate_episode')
    started = [e['episode_id'] for e in events if e['event'] == 'TARGET_STARTED']
    completed = [e['episode_id'] for e in events if e['event'] == 'TARGET_COMPLETED']
    require(Counter(started) == Counter(completed) == Counter(expected_ids), 'new_execution_events_mismatch')
    for name, field, expected in (('INITIAL_EXECUTION_REUSED', 'episode_id', h1['episode_id']),
                                  ('INITIAL_GENERATION_REUSED', 'generation_id', initial['generation']['generation_id'])):
        reused = [e for e in events if e['event'] == name]
        require(len(reused) == 1 and reused[0][field] == expected, 'reuse_event_mismatch')
    require(decisions and decisions[-1]['action'] == 'Stop', 'missing_terminal_stop')
    outcomes = read(directory / 'outcomes.json')
    require(len(outcomes) == 1, 'outcome_count_mismatch')
    outcome = outcomes[0]
    require(outcome['case_id'] == cid and outcome['policy_id'] == 'always_revise', 'outcome_identity_mismatch')
    require(outcome['target_attempts'] == len(branch) and outcome['shared_h1_id'] == h1['episode_id'], 'outcome_budget_mismatch')
    require(outcome['initial_gold'] is False, 'initial_gold_mismatch')
    policy = AlwaysRevise()
    for decision in decisions:
        spent = decision['spent_target_budget']
        require(type(spent) is int and 0 <= spent <= len(branch) <= 2, 'invalid_spent_budget')
        require(decision['remaining_target_budget'] == 2-spent, 'remaining_budget_mismatch')
        visible = episodes[:spent+1]
        require(decision['visible_history_ids'] == [e['episode_id'] for e in visible], 'future_or_foreign_history')
        history = History.from_episodes([Episode(**e) for e in visible], Budget(2, spent))
        require(decision['history'] == history.summary(), 'history_summary_mismatch')
        if decision['reason'] == 'GENERATION_ERROR':
            require(decision['action'] == 'Stop' and outcome['status'] == 'GENERATION_ERROR', 'error_stop_mismatch')
        else:
            expected = policy.decide(history)
            require((decision['action'], decision['reason']) == (expected.action.value, expected.reason), 'policy_decision_mismatch')
    revisions = [d for d in decisions if d['action'] == 'Revise']
    require(len(revisions) <= 2 and len(revisions) == outcome['revisions'], 'revision_count_mismatch')
    generations_started = [e['generation_id'] for e in events if e['event'] == 'GENERATION_STARTED']
    generation_completed = [e for e in events if e['event'] == 'GENERATION_COMPLETED']
    require(generations_started == [f'{cid}/always_revise/P{i}' for i in range(1, len(revisions)+1)], 'generation_sequence_mismatch')
    require([e['generation_id'] for e in generation_completed] == generations_started, 'generation_completion_mismatch')
    attempts = [deepcopy(initial['attempt'])]
    for index, event in enumerate(generation_completed):
        record = read(directory / event['raw_path'])
        require(record['generation_id'] == event['generation_id'] and record['status'] == event['status'], 'generation_event_mismatch')
        require(record['routing'] == initial['generation']['routing'], 'routing_changed')
        require(len(record['calls']) == 1 and record['calls'][0]['stage'] == 'attacker_revise', 'new_routing_or_extra_generation_call')
        require(record['calls'][0]['system_prompt'] == (ROOT / 'src/vendor/attacker_role.txt').read_text(), 'system_prompt_changed')
        require(record['calls'][0]['prompt'] == expected_prompt(case, initial['route']['document'], attempts), 'revision_prompt_history_mismatch')
        if record['status'] == 'GENERATION_ERROR':
            require(index == len(branch) and outcome['status'] == 'GENERATION_ERROR', 'generation_error_position_mismatch')
        else:
            require(index < len(branch), 'ready_generation_without_execution')
            episode = branch[index]
            saved = read(directory / episode['raw_path'])
            require(record['payload'] == saved['payload'] and record['payload_sha256'] == episode['payload_sha256']
                    and payload_hash(record['payload']) == record['payload_sha256'], 'generation_execution_payload_mismatch')
            result = saved['result']
            attempts.append(dict(analysis=record['analysis'], injection=saved['payload'],
                security=result['gold'], utility=result['utility'],
                final_output=(result['black_box_final'] if result['black_box_final'] is not None else result['final'])[:4000]))
    success = any(e['gold'] is True for e in episodes)
    invalid = outcome['status'] in ('TARGET_ERROR', 'GENERATION_ERROR')
    require(outcome['gold_success'] is (True if success else None if invalid else False), 'outcome_success_mismatch')
    if outcome['status'] != 'GENERATION_ERROR':
        require(outcome['status'] == episodes[-1]['status'], 'terminal_status_mismatch')
    replay = [verify_episode(directory, case, e, config) for e in episodes]
    metrics = read(directory / 'metrics.json')
    require(metrics['physical_total_cost']['target_executions'] == len(branch), 'new_cost_mismatch')
    require(metrics['attributed_total_cost']['target_executions'] == len(episodes), 'attributed_cost_mismatch')
    require(metrics['physical_total_cost']['generation_requests'] == len(revisions), 'generation_cost_mismatch')
    require(metrics['reused_initial_executions'] == 1 and metrics['physical_initial_executions'] == 0, 'reuse_cost_mismatch')
    return dict(case_id=cid, status='verified', new_target_executions=len(branch), reused_initial_executions=1,
                revision_prompts_verified=len(revisions), decisions_replayed=len(decisions), outcomes=outcomes, episodes=replay)


def verify_run(path):
    path = Path(path).resolve()
    manifest = read(path / 'manifest.json')
    cases = check_frozen(manifest)
    source = Path(manifest['source_run'])
    source_manifest = read(source / 'manifest.json')
    check_frozen(source_manifest)
    require(cases == manifest['cases'] == source_manifest['cases'], 'case_snapshot_mismatch')
    require([c['case_id'] for c in cases] == CASE_IDS, 'wrong_five_cases')
    require(manifest['source_manifest_sha256'] == digest(source / 'manifest.json'), 'source_manifest_changed')
    require(manifest['config']['post_failure_target_budget'] == 2
            and manifest['config']['policies'] == {'always_revise': {}}, 'wrong_asr3_protocol')
    require(manifest['asr_k'] == 3 and manifest['reused_initial'] is True, 'reuse_not_declared')
    issues, rows = [], []
    if read(path / 'status.json')['status'] != 'completed':
        issues.append(dict(code='run_not_completed'))
    workers = read(path / 'worker_results.json') if (path / 'worker_results.json').exists() else []
    if len(workers) != 5 or {w['case_id'] for w in workers} != set(CASE_IDS):
        issues.append(dict(code='missing_worker_results'))
    for case in cases:
        try:
            row = verify_case(path / case['case_id'], case, manifest)
            worker = next(w for w in workers if w['case_id'] == case['case_id'])
            require(worker['status'] == 'completed' and worker['outcomes'] == row['outcomes'], 'worker_outcome_mismatch')
            require(worker['new_target_executions'] == row['new_target_executions'] and worker['reused_initial_executions'] == 1, 'worker_cost_mismatch')
            rows.append(row)
        except Exception as exc:
            issues.append(dict(case_id=case['case_id'], code=str(exc) if isinstance(exc, VerificationError) else type(exc).__name__))
    episodes = [e for row in rows for e in row['episodes']]
    return dict(status='passed' if not issues and len(rows) == 5 else 'failed', api_calls=0,
        source=str(path), verified_cases=len(rows), expected_cases=5,
        native_scores_recomputed=sum(e['status'] == 'native_scores_recomputed' for e in episodes),
        new_target_executions=sum(r['new_target_executions'] for r in rows),
        reused_initial_executions=sum(r['reused_initial_executions'] for r in rows),
        revision_prompts_verified=sum(r['revision_prompts_verified'] for r in rows),
        decisions_replayed=sum(r['decisions_replayed'] for r in rows), issues=issues, cases=rows,
        manifest_sha256=digest(path / 'manifest.json'),
        limitations=['No new API executions.', 'Receiver evidence is replayed from saved legal views, not independently recaptured.',
                    'Integrity checks do not establish decision efficacy or held-out generalization.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = verify_run(args.run)
    write_json(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('cases', 'limitations')}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
