"""Summarize the five-case live pilot and recompute every valid official score offline."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.replay_gold import restore_state
from src.adapters.agentdojo import official_scores
from src.engine.config import check_frozen, digest
from src.engine.storage import plain, write_json
from agentdojo.functions_runtime import FunctionCall
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.task_suite.task_suite import model_output_from_messages, functions_stack_trace_from_messages

RUN = ROOT / 'runs/pilot5_original_stage_aware_20261028'


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main():
    if json.loads((RUN / 'status.json').read_text())['status'] != 'completed':
        raise RuntimeError('Run is not complete; do not report partial results as final')
    manifest = json.loads((RUN / 'manifest.json').read_text())
    cases = check_frozen(manifest)
    by_id = {c['case_id']: c for c in cases}
    episodes = read_jsonl(RUN / 'episodes.jsonl')
    decisions = read_jsonl(RUN / 'decisions.jsonl')
    outcomes = json.loads((RUN / 'outcomes.json').read_text())
    assert len(cases) == len(outcomes) == 5
    assert len(episodes) <= 20
    replay = []
    for episode in episodes:
        raw_path = RUN / episode['raw_path']
        result = json.loads(raw_path.read_text())['result']
        assert episode['gold'] == result['gold']
        if result['error']:
            assert result['gold'] is None
            replay.append(dict(episode_id=episode['episode_id'], status='target_error', gold=None))
            continue
        case = by_id[episode['case_id']]
        suite = get_suite(manifest['config']['target']['benchmark_version'], case['suite_name'])
        raw = result['raw']
        pre, post = [restore_state(suite.environment_type, raw[name]) for name in ('pre', 'post')]
        assert [plain(pre), plain(post)] == [raw['pre'], raw['post']], 'State roundtrip mismatch'
        messages = raw['messages']
        for message in messages:
            if message['role'] == 'assistant':
                message['tool_calls'] = [FunctionCall.model_validate(t) for t in message.get('tool_calls') or []]
        utility, gold = official_scores(suite, suite.get_user_task_by_id(case['user_task_id']),
            suite.get_injection_task_by_id(case['injection_task_id']), model_output_from_messages(messages) or [],
            pre, post, functions_stack_trace_from_messages(messages))
        assert (utility, gold) == (result['utility'], result['gold']), episode['episode_id']
        replay.append(dict(episode_id=episode['episode_id'], status='recomputed', utility=utility, gold=gold,
                           raw_sha256=digest(raw_path)))
    rows = []
    for outcome in outcomes:
        cid = outcome['case_id']; case = by_id[cid]
        attempts = [e for e in episodes if e['case_id'] == cid]
        assert len(attempts) <= 4
        successful = next((i for i, e in enumerate(attempts, 1) if e['gold'] is True), None)
        assert (successful is not None) == (outcome['gold_success'] is True)
        rows.append(dict(case_id=cid, suite=case['suite_name'], user_task=case['user_task_id'],
            injection_task=case['injection_task_id'], goal=case['injection_task_goal'],
            success=outcome['gold_success'], status=outcome['status'], first_success_attempt=successful,
            target_executions=len(attempts), revisions=outcome.get('revisions', 0),
            gold_sequence=[e['gold'] for e in attempts], trigger_sequence=[e['trigger'] for e in attempts],
            payload_ids=[e['payload_id'] for e in attempts],
            actions=[dict(action=d['action'], reason=d['reason']) for d in decisions if d['case_id'] == cid]))
    metrics = json.loads((RUN / 'metrics.json').read_text())
    report = dict(status='completed_and_scores_verified', kind='LIVE_DEVELOPMENT_RETEST',
        target=manifest['config']['target'], attacker=manifest['config']['attacker'], policy='stage_aware',
        cases=5, confirmed_successes=sum(r['success'] is True for r in rows),
        valid_failures=sum(r['success'] is False for r in rows), invalid_cases=sum(r['success'] is None for r in rows),
        confirmed_case_success_rate=sum(r['success'] is True for r in rows)/5,
        target_executions=len(episodes), recomputed_scores=sum(r['status']=='recomputed' for r in replay),
        rows=rows, cost=metrics['physical_total_cost'], preflight_cost=metrics['preflight_cost'],
        score_replay=replay, output=str(RUN),
        limitations=['Five historically evaluated identities, selected by saved index without outcome filtering',
            'One Stage-Aware run; no baseline comparison or independent held-out efficacy claim',
            'Missing usage is not zero cost; monetary cost is not inferred'])
    write_json(ROOT / 'reports/pilot5_results_20261028.json', report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('target','attacker','cost','preflight_cost','score_replay')},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
