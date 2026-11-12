#!/usr/bin/env python3
"""Read-only verification of actual paired generation/Target records."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_revise_ablation_20261030 import ARMS, canonical_hash, require, verify_plan
from src.engine.storage import write_json
from src.vendor.prompts import _format_attempt


def read(path):
    return json.loads(path.read_text())


def verify(run):
    plan = read(run / 'manifest.json')
    verify_plan(plan)
    require(read(run / 'status.json')['status'] in ('completed', 'completed_with_errors'), 'Run is not terminal')
    rows = read(run / 'results.json')
    require([r['case_id'] for r in rows] == plan['case_ids'], 'Case population changed')
    sessions, generation_calls, targets, success, errors, missing = set(), 0, 0, 0, 0, 0
    for row in rows:
        cid = row['case_id']
        require(row['source_history_sha256'] == plan['sources'][cid]['history_sha256'], 'Source history changed')
        require(set(row['arms']) == set(ARMS), 'Arm missing from case record')
        documents = {}
        for arm, record in row['arms'].items():
            directory = run / cid / arm
            generation = read(directory / 'raw' / cid / arm / 'P1.generation.json')
            require(generation == record['generation'], 'Generation copies disagree')
            require(generation['experience']['entries'] == 0, 'Experience contamination')
            require(generation['history_iters'] == [0] and generation['prompt_mode'] == 'full', 'Unexpected history')
            session = generation['session_id']
            require(session not in sessions, 'Session reused across arms/cases')
            sessions.add(session)
            calls = generation['calls']
            require(len(calls) == 1 and calls[0]['stage'] == 'attacker_revise', 'Unexpected model calls')
            generation_calls += len(calls)
            require(_format_attempt(plan['sources'][cid]['attempt'], True) in calls[0]['prompt'], 'Different source feedback')
            imported = read(directory / 'raw' / cid / 'shared/P0.generation.json')
            require(not imported['calls'] and imported['payload_sha256'] == plan['sources'][cid]['payload_sha256'], 'P0 resampled')
            if arm != 'full_original':
                documents[arm] = generation['routing']['a2']['strategy_document_sha256']
            events = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines() if line.strip()]
            starts = [e['episode_id'] for e in events if e['event'] == 'TARGET_STARTED']
            ends = [e['episode_id'] for e in events if e['event'] == 'TARGET_COMPLETED']
            require(len(starts) == len(set(starts)) and starts == ends, 'Duplicate or unfinished Target execution')
            trials = sorted(record['trials'], key=lambda t: t['repeat'])
            require([t['repeat'] for t in trials] == [0, 1], 'Planned scoring slots changed')
            for trial in trials:
                if 'episode_id' not in trial:
                    require(trial['gold'] is None, 'Unstarted slot cannot carry Gold')
                    missing += 1
                    continue
                require(trial['episode_id'] in starts, 'Execution has no durable start')
                raw = read(directory / trial['raw_path'])
                require(raw['result']['gold'] is trial['gold'] and raw['result']['error'] == trial['error'], 'Raw Gold/error disagrees')
                require(trial['payload_sha256'] == generation['payload_sha256'], 'Candidate changed across scoring repetitions')
                require(raw['payload'] == generation['payload'], 'Scored payload differs from generation')
                targets += 1
                success += trial['gold'] is True
                errors += trial['error'] is not None
        require(documents['a2_v2'] == documents['a2_v2_calibrated'], 'Calibration arm changed retrieval')
    amendment = run / 'parallel_handoff_20261030'
    terminal_errors = []
    if amendment.exists():
        require(read(amendment / 'status.json')['status'] == 'completed', 'Parallel coordinator not terminal')
        for claim in read(amendment / 'manifest.json')['new_schedule']['only_claimed_cases']:
            if claim['claimed']:
                terminal = read(amendment / (claim['case_id'] + '.terminal.json'))
                if terminal['status'] != 'completed':
                    terminal_errors.append(terminal)
                    result = next(row for row in rows if row['case_id'] == claim['case_id'])
                    require(result['status'] == 'worker_error', 'Parallel worker error was hidden')
    require(generation_calls <= 20 and targets + missing == 40, 'Budget changed')
    return dict(status='passed', data_and_environment_unchanged=True,
        independent_sessions=len(sessions), revision_model_calls=generation_calls,
        target_executions=targets, planned_target_slots=40, unstarted_slots=missing,
        confirmed_successes=success, target_errors=errors, terminal_errors=terminal_errors,
        same_history=True, same_p0=True, calibration_retrieval_identical=True,
        generation_and_raw_gold_consistent=True, duplicate_target_executions=0,
        api_calls_by_this_audit=0, interpretation='Integrity checks do not establish generalization or effect significance.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=ROOT / 'runs/revise_ablation_20261030')
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/revise_ablation_integrity_20261030.json')
    args = parser.parse_args()
    report = verify(args.run)
    write_json(args.output, report)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
