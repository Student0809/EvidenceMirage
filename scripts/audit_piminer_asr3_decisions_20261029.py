"""Read-only policy replay on the new baseline history, with no counterfactual execution."""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.engine.budget import Budget
from src.engine.history import History
from src.engine.storage import write_json
from src.policies.stage_aware import StageAware
from src.schema import Episode


def replay(run):
    run = Path(run).resolve()
    manifest = json.loads((run / 'manifest.json').read_text())
    source = json.loads((Path(manifest['source_run']) / 'manifest.json').read_text())
    policy = StageAware(**source['config']['policies']['stage_aware'])
    rows = []
    for case in manifest['cases']:
        directory = run / case['case_id']
        episodes = {e['episode_id']: Episode(**e) for e in (
            json.loads(line) for line in (directory / 'episodes.jsonl').read_text().splitlines())}
        for line in (directory / 'decisions.jsonl').read_text().splitlines():
            decision = json.loads(line)
            if decision['reason'] == 'GENERATION_ERROR':
                continue
            history = [episodes[eid] for eid in decision['visible_history_ids']]
            spent = decision['spent_target_budget']
            shadow = policy.decide(History.from_episodes(history, Budget(2, spent)))
            last = history[-1]
            rows.append(dict(case_id=case['case_id'], decision_id=decision['decision_id'],
                last_episode_id=last.episode_id, gold=last.gold, trigger=last.trigger,
                remaining_target_budget=2-spent, actual_baseline_action=decision['action'],
                stage_aware_action=shadow.action.value, stage_aware_reason=shadow.reason,
                differs=decision['action'] != shadow.action.value))
    return dict(status='offline_policy_replay', api_calls=0, run=str(run), decisions=rows,
        different_actions=[r for r in rows if r['differs']],
        retry_opportunities=[r for r in rows if r['stage_aware_action'] == 'Retry'],
        counterfactual_execution_performed=False, counterfactual_reward=None,
        interpretation='Policy actions on observed baseline histories only; unexecuted choices have no observed reward.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = replay(args.run)
    write_json(args.output, report)
    print(json.dumps(dict(decisions=len(report['decisions']), different_actions=report['different_actions'],
        counterfactual_execution_performed=False), ensure_ascii=False))


if __name__ == '__main__':
    main()
