"""Read-only verification of the five-case live pilot; never calls any model API.

Rebuild native pre/post states and call the original suite checkers. Re-query the
captured legal receiver view and reconstruct Trigger. This checks saved evidence
consistency, not an independent reconstruction of event capture from tool traces.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agentdojo.default_suites
from agentdojo.functions_runtime import FunctionCall
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.task_suite.task_suite import model_output_from_messages, functions_stack_trace_from_messages
from scripts.replay_gold import restore_state
from src.engine.budget import Budget
from src.engine.config import data_fingerprints, check_frozen, digest
from src.engine.history import History
from src.engine.storage import plain, write_json
from src.evidence.ledger import EvidenceLedger
from src.evidence.observer import FixedObserver, policy_for_case
from src.evidence.stage import estimate_stage, response_signal
from src.policies.registry import build_policies
from src.schema import Episode, payload_hash
from src.vendor import attacker_observer as native

EXPECTED_CASES = ['c000', 'c001', 'c004', 'c005', 'c006']
EXPECTED_ARMS = {'stage_aware', 'stage_aware_no_evidence'}


class VerificationError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def read(path):
    return json.loads(Path(path).read_text())


def read_lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def verify_episode(directory, case, episode, config):
    eid = episode['episode_id']
    raw_path = (directory / episode['raw_path']).resolve()
    require(raw_path.is_relative_to(directory.resolve()), 'raw_path_outside_case')
    saved = read(raw_path)
    result = saved['result']
    require(payload_hash(saved['payload']) == episode['payload_sha256'], 'payload_hash_mismatch')
    require(episode['case_id'] == case['case_id'], 'episode_case_mismatch')
    for field in ('gold', 'final', 'observation', 'error', 'observation_error'):
        require(episode[field] == result[field], 'raw_episode_' + field + '_mismatch')
    require(result['raw'].get('episode_id', eid) == eid, 'raw_episode_id_mismatch')
    if result['error'] is not None:
        require(result['gold'] is None and episode['status'] == 'TARGET_ERROR', 'target_error_not_null')
        require(not saved['query_packets'] and result['observation'] is None, 'target_error_has_evidence')
        score = dict(status='target_error_preserved', gold=None, utility=None)
    else:
        require(type(result['gold']) is bool and type(result['utility']) is bool, 'valid_score_not_boolean')
        require(episode['status'] == ('VALID_GOLD_TRUE' if result['gold'] else 'VALID_GOLD_FALSE'), 'valid_status_mismatch')
        suite = get_suite(config['target']['benchmark_version'], case['suite_name'])
        require(case['user_task_prompt'] == suite.get_user_task_by_id(case['user_task_id']).PROMPT,
                'native_user_prompt_mismatch')
        require(case['injection_task_goal'] == suite.get_injection_task_by_id(case['injection_task_id']).GOAL,
                'native_goal_mismatch')
        raw = result['raw']
        pre, post = [restore_state(suite.environment_type, raw[name]) for name in ('pre', 'post')]
        require([plain(pre), plain(post)] == [raw['pre'], raw['post']], 'state_roundtrip_mismatch')
        messages = deepcopy(raw['messages'])
        for message in messages:
            if message['role'] == 'assistant':
                message['tool_calls'] = [FunctionCall.model_validate(call)
                                         for call in message.get('tool_calls') or []]
        output = model_output_from_messages(messages)
        require(output is not None, 'valid_episode_missing_final_output')
        trace = functions_stack_trace_from_messages(messages)
        # Call native suite checkers directly, independently of saved scores.
        utility = bool(suite._check_task_result(suite.get_user_task_by_id(case['user_task_id']),
                                              output, pre, post, trace))
        gold = bool(suite._check_task_result(suite.get_injection_task_by_id(case['injection_task_id']),
                                           output, pre, post, trace))
        require((utility, gold) == (result['utility'], result['gold']), 'official_score_mismatch')
        score = dict(status='native_scores_recomputed', gold=gold, utility=utility)
    view = result['observation']
    if view is not None:
        native.validate_view(view, policy_for_case(case))
        if 'event_coverage' in view:
            suite = get_suite(config['target']['benchmark_version'], case['suite_name'])
            require(native._audit_native_tools(config['target']['benchmark_version'], case['suite_name'], suite.tools),
                    'native_complete_coverage_source_mismatch')
    packets = FixedObserver(**config['observation']).collect(eid, case, view)
    require(plain(packets) == saved['query_packets'], 'query_packets_mismatch')
    ledger = EvidenceLedger(eid, case['goal_spec'])
    for packet in packets:
        ledger.update(packet)
    require(ledger.trigger == episode['trigger'], 'trigger_mismatch')
    require(plain(ledger.records()) == episode['evidence'], 'evidence_records_mismatch')
    require(sum(p['charged_cost'] for p in packets) == episode['query_units'], 'query_cost_mismatch')
    require(estimate_stage(result['gold'], ledger.trigger, result['error']) == episode['stage'], 'stage_mismatch')
    require(response_signal(result['final']) == episode['response_signal'], 'response_signal_mismatch')
    return dict(episode_id=eid, **score, trigger=ledger.trigger,
                query_ids=[p['query_id'] for p in packets], raw_sha256=digest(raw_path))


def verify_case(directory, case, manifest):
    config = manifest['config']
    cid = case['case_id']
    child = read(directory / 'manifest.json')
    for key in ('config', 'environment'):
        require(child[key] == manifest[key], 'child_manifest_' + key + '_mismatch')
    require(data_fingerprints(child['inputs_sha256']) == data_fingerprints(manifest['inputs_sha256']),
            'child_manifest_inputs_sha256_mismatch')
    require(child['cases'] == [case] and child['case_ids'] == [cid], 'child_case_snapshot_mismatch')
    require(read(directory / 'status.json')['status'] == 'completed', 'case_not_completed')
    require(case['goal_spec']['trigger_ids'], 'no_observable_necessary_trigger')
    episodes = read_lines(directory / 'episodes.jsonl')
    events = read_lines(directory / 'events.jsonl')
    decisions = read_lines(directory / 'decisions.jsonl')
    outcomes = read(directory / 'outcomes.json')
    by_id = {e['episode_id']: e for e in episodes}
    require(len(by_id) == len(episodes), 'duplicate_episode')
    started = [e['episode_id'] for e in events if e['event'] == 'TARGET_STARTED']
    completed = [e['episode_id'] for e in events if e['event'] == 'TARGET_COMPLETED']
    require(Counter(started) == Counter(completed) == Counter(by_id.keys()), 'started_completed_episode_mismatch')
    for event in events:
        if event['event'] == 'TARGET_STARTED':
            require(event['payload_sha256'] == by_id[event['episode_id']]['payload_sha256'], 'started_payload_mismatch')
    require(len(outcomes) == 2 and {o['policy_id'] for o in outcomes} == EXPECTED_ARMS, 'missing_or_duplicate_arm')
    require(all(o['case_id'] == cid for o in outcomes), 'outcome_case_mismatch')
    require(all(e['policy_id'] in EXPECTED_ARMS | {'shared'} for e in episodes), 'unexpected_episode_arm')
    shared = [e for e in episodes if e['policy_id'] == 'shared']
    if not shared:
        require(not episodes and not decisions, 'missing_shared_h1')
        require(all(o['status'] == 'GENERATION_ERROR' and o['gold_success'] is None
                    and o['initial_gold'] is None and o['target_attempts'] == 0 for o in outcomes),
                'initial_generation_failure_mismatch')
        return dict(case_id=cid, status='initial_generation_error_preserved', episodes=[], outcomes=outcomes,
                    target_executions=0, decision_checks=0)
    require(len(shared) == 1, 'shared_h1_not_unique')
    h1 = shared[0]
    require(h1['episode_id'] == cid + '/shared/000' and h1['phase'] == 'initial', 'invalid_shared_h1_identity')
    require(episodes[0]['episode_id'] == h1['episode_id'], 'shared_h1_not_first')
    replay = [verify_episode(directory, case, episode, config) for episode in episodes]
    policies = build_policies(config['policies'])
    for outcome in outcomes:
        pid = outcome['policy_id']
        branch = [e for e in episodes if e['policy_id'] == pid]
        require(len(branch) <= 3 and outcome['target_attempts'] == len(branch), 'arm_budget_mismatch')
        require([e['episode_id'] for e in branch] == [f'{cid}/{pid}/{i:03d}' for i in range(1, len(branch)+1)],
                'branch_episode_sequence_mismatch')
        require(all(e['phase'] == 'post_failure' for e in branch), 'branch_phase_mismatch')
        require(outcome['shared_h1_id'] == h1['episode_id'] and outcome['initial_gold'] == h1['gold'],
                'shared_h1_outcome_mismatch')
        branch_decisions = [d for d in decisions if d['policy_id'] == pid]
        require(branch_decisions and branch_decisions[0]['visible_history_ids'] == [h1['episode_id']],
                'initial_branch_history_mismatch')
        require(branch_decisions[-1]['action'] == 'Stop', 'missing_terminal_stop')
        for decision in branch_decisions:
            spent = decision['spent_target_budget']
            require(type(spent) is int and 0 <= spent <= len(branch) <= 3, 'invalid_spent_budget')
            require(decision['remaining_target_budget'] == 3 - spent, 'remaining_budget_mismatch')
            expected_ids = [h1['episode_id']] + [e['episode_id'] for e in branch[:spent]]
            require(decision['visible_history_ids'] == expected_ids, 'foreign_future_or_missing_branch_history')
            history = History.from_episodes([Episode(**by_id[eid]) for eid in expected_ids], Budget(3, spent))
            require(decision['history'] == history.summary(), 'decision_history_summary_mismatch')
            if decision['reason'] == 'GENERATION_ERROR':
                require(decision['action'] == 'Stop' and outcome['status'] == 'GENERATION_ERROR', 'generation_error_stop_mismatch')
            else:
                expected = policies[pid].decide(history)
                require((decision['action'], decision['reason']) == (expected.action.value, expected.reason),
                        'policy_decision_mismatch')
                if decision['action'] == 'Retry' and spent < len(branch):
                    require(branch[spent]['payload_sha256'] == history.last.payload_sha256, 'retry_changed_payload')
        history_episodes = [h1] + branch
        success = any(e['gold'] is True for e in history_episodes)
        invalid = outcome['status'] in ('TARGET_ERROR', 'GENERATION_ERROR')
        expected_success = True if success else None if invalid else False
        require(outcome['gold_success'] is expected_success, 'outcome_success_mismatch')
        if outcome['status'] != 'GENERATION_ERROR':
            require(outcome['status'] == history_episodes[-1]['status'], 'terminal_status_mismatch')
    require(len(episodes) <= 7, 'case_budget_exceeded')
    metrics = read(directory / 'metrics.json')
    require(metrics['physical_total_cost']['target_executions'] == len(episodes), 'metrics_target_count_mismatch')
    return dict(case_id=cid, status='verified', episodes=replay, outcomes=outcomes,
                target_executions=len(episodes), decision_checks=len(decisions),
                shared_h1_id=h1['episode_id'], shared_payload_sha256=h1['payload_sha256'])


def verify_run(source):
    source = Path(source).resolve()
    manifest = read(source / 'manifest.json')
    cases = check_frozen(manifest)
    require([c['case_id'] for c in cases] == EXPECTED_CASES, 'wrong_five_cases')
    require(manifest['cases'] == cases, 'parent_case_snapshot_mismatch')
    require(set(manifest['config']['policies']) == EXPECTED_ARMS, 'wrong_controller_arms')
    require(manifest['config']['post_failure_target_budget'] == 3, 'wrong_budget')
    status = read(source / 'status.json')['status']
    issues, rows = [], []
    if status != 'completed':
        issues.append(dict(code='run_not_completed', run_status=status))
    workers = read(source / 'worker_results.json') if (source / 'worker_results.json').exists() else []
    if {r['case_id'] for r in workers} != set(EXPECTED_CASES) or len(workers) != 5:
        issues.append(dict(code='missing_worker_results'))
    for case in cases:
        try:
            row = verify_case(source / case['case_id'], case, manifest)
            worker = next((r for r in workers if r['case_id'] == case['case_id']), None)
            require(worker and worker['status'] == 'completed', 'worker_not_completed')
            require(worker['target_executions'] == row['target_executions'] and worker['outcomes'] == row['outcomes'],
                    'worker_result_mismatch')
            rows.append(row)
        except Exception as exc:
            issues.append(dict(case_id=case['case_id'], code=str(exc) if isinstance(exc, VerificationError)
                               else type(exc).__name__))
    target_count = sum(r['target_executions'] for r in rows)
    require(target_count <= 35, 'total_budget_exceeded')
    episodes = [e for r in rows for e in r['episodes']]
    return dict(status='passed' if not issues and len(rows) == 5 else 'failed', api_calls=0,
        source=str(source), verified_cases=len(rows), expected_cases=5,
        native_scores_recomputed=sum(e['status'] == 'native_scores_recomputed' for e in episodes),
        target_errors_preserved=sum(e['status'] == 'target_error_preserved' for e in episodes),
        verified_target_executions=target_count, decisions_replayed=sum(r['decision_checks'] for r in rows),
        scope='Original native scores from restored raw states; legal-view packets, Trigger, policy decisions, shared history and budgets.',
        limitations=['No new model execution; no resampling.',
                     'Evidence consistency is checked against the saved legal receiver view, not independently recaptured from traces.',
                     'Passing integrity checks does not establish efficacy or independent held-out generalization.'],
        manifest_sha256=digest(source / 'manifest.json'),
        issues=issues, cases=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=str(ROOT / 'runs/observable_pilot5_20261029'))
    parser.add_argument('--output', default=str(ROOT / 'reports/observable_pilot5_verification_20261029.json'))
    args = parser.parse_args()
    report = verify_run(args.source)
    write_json(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('cases', 'limitations')}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
