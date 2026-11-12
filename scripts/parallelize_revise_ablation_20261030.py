#!/usr/bin/env python3
"""Claim only unstarted cases and parallelize independent arms without resampling."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from run_revise_ablation_20261030 import (
    ARMS, Episode, FixedObserver, Runner, RunStore, AlwaysRevise, canonical_hash,
    configure_credentials, counterbalanced_orders, now, read, require, summarize,
    verify_plan, write_json)

CLAIM_CASES = ('c005', 'c006')
AMENDMENT_DIR = 'parallel_handoff_20261030'
USER_INSTRUCTION = '可用并行调用API来提速'


def parent_identity(pid):
    path = Path('/proc') / str(pid)
    try:
        stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        command = (path / 'cmdline').read_bytes()
    except FileNotFoundError:
        return None
    if stat[0] == 'Z':
        return None
    return dict(pid=pid, start_ticks=stat[19], command_sha256=hashlib.sha256(command).hexdigest(),
                arguments=[x.decode() for x in command.split(b'\0') if x])


def validate_parent(pid, run):
    identity = parent_identity(pid)
    require(identity is not None, 'Original parent is not running')
    args = identity['arguments']
    require(any(Path(arg).name == 'run_revise_ablation_20261030.py' for arg in args),
            'PID is not the original experiment parent')
    require('--output' in args and Path(args[args.index('--output') + 1]).resolve() == run,
            'Original parent belongs to another output directory')
    return identity


def parent_active(expected):
    current = parent_identity(expected['pid'])
    return current is not None and current['start_ticks'] == expected['start_ticks']


def pool_workers(pid):
    children = Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
    identities = [parent_identity(int(child)) for child in children]
    return [entry for entry in identities if entry and any('spawn_main' in arg for arg in entry['arguments'])]


def claim_case(run, cid, coordination_id):
    """The existing worker opens this same log with mode x before any API call."""
    directory, log = run / cid, run / (cid + '.log')
    if directory.exists() or log.exists():
        return dict(case_id=cid, claimed=False, reason='case_or_log_already_exists')
    try:
        with log.open('x') as stream:
            json.dump(dict(event='PARALLEL_HANDOFF_CLAIM', coordination_id=coordination_id,
                           pid=os.getpid(), case_id=cid, timestamp=now()), stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return dict(case_id=cid, claimed=False, reason='exclusive_log_claim_lost')
    try:
        directory.mkdir(exist_ok=False)
    except FileExistsError:
        # A competing actor already owns the directory. Keep the claim log as a
        # durable conflict record; never touch that directory or start requests.
        return dict(case_id=cid, claimed=False, reason='directory_race_after_log_claim',
                    created_log=True)
    claim = dict(case_id=cid, claimed=True, coordination_id=coordination_id,
                 directory=str(directory), log=str(log), timestamp=now())
    write_json(directory / 'handoff_claim.json', claim)
    return claim


def execute_parallel_case(plan, cid, run, coordination_id, adapter_factory=None,
                          observer_factory=None):
    if adapter_factory is None:
        from main import adapters
        adapter_factory = adapters
    observer_factory = observer_factory or (lambda options: FixedObserver(**options))
    verify_plan(plan)
    directory = Path(run) / cid
    claim = read(directory / 'handoff_claim.json')
    require(claim['coordination_id'] == coordination_id and claim['claimed'], 'Case claim differs')
    require(not (directory / 'result.json').exists(), 'Claimed case already has execution records')
    case = next(c for c in plan['cases'] if c['case_id'] == cid)
    source = plan['sources'][cid]
    require(canonical_hash([source['attempt']]) == source['history_sha256'], 'History projection changed')
    order, trial_orders = counterbalanced_orders(plan['case_ids'].index(cid), plan['seed'])
    result = dict(case_id=cid, status='generating', source_history_sha256=source['history_sha256'],
        generation_order=order, scoring_orders=trial_orders, arms={},
        scheduling_amendment=dict(coordination_id=coordination_id,
            generation='Four independent arms submitted concurrently in original order.',
            scoring='Four independent arms per repeat; barrier between the two repeats.'))
    write_json(directory / 'result.json', result)
    write_json(directory / 'status.json', dict(status='generating'))

    def generate(arm):
        config = deepcopy(plan['arm_configs'][arm])
        target, attacker = adapter_factory(config)
        store = RunStore(directory / arm, dict(kind=plan['kind'], case_id=cid, arm=arm,
            source_history_sha256=source['history_sha256'], config=config,
            environment=plan['environment'], independent_heldout=False,
            coordination_id=coordination_id))
        runner = Runner(target, attacker, observer_factory(config['observation']),
            {arm: AlwaysRevise()}, store, post_failure_budget=2, seed=plan['seed'])
        p0, _ = runner.generate(case, 'shared', None, None, 'P0')
        require(p0 is not None and p0.sha256 == source['payload_sha256'], 'Imported P0 differs')
        h1 = Episode(**deepcopy(source['episode']))
        runner.attempts[h1.episode_id] = {k: deepcopy(v) for k, v in source['attempt'].items() if k != 'iter'}
        runner.packets[h1.episode_id] = deepcopy(source['packets'])
        attacker.begin_branch(case, arm)
        p1, generation = runner.generate(case, arm, p0, h1, 'P1', history=[h1])
        if generation.get('experience') is not None:
            require(generation['experience']['entries'] == 0, 'Nonempty rolling experience')
        record = dict(generation_status=generation['status'], generation=generation,
            unchanged_payload=p1.sha256 == p0.sha256 if p1 else None,
            payload_sha256=p1.sha256 if p1 else None, trials=[])
        return runner, p1, record

    runners, candidates, infrastructure_errors, unresolved = {}, {}, [], set()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(generate, arm): arm for arm in order}
        for future in as_completed(futures):
            arm = futures[future]
            try:
                runner, p1, record = future.result()
                runners[arm], candidates[arm] = runner, p1
                result['arms'][arm] = record
            except Exception as exc:
                infrastructure_errors.append(dict(arm=arm, phase='generation_setup', error_type=type(exc).__name__))
                candidates[arm] = None
                result['arms'][arm] = dict(generation_status='GENERATION_ERROR',
                    infrastructure_error=type(exc).__name__, trials=[])
            write_json(directory / 'result.json', result)
        result['status'] = 'scoring'
        write_json(directory / 'status.json', dict(status='scoring'))

        def score(arm, repeat):
            if candidates[arm] is None:
                status = 'NOT_STARTED_PRIOR_UNRESOLVED_EXECUTION' if arm in unresolved else 'NOT_STARTED_GENERATION_ERROR'
                return dict(repeat=repeat, status=status, gold=None)
            episode = runners[arm].execute(case, arm, candidates[arm], 'fixed_p1_scoring', repeat + 1)
            return dict(repeat=repeat, **episode.to_dict())

        for repeat, trial_order in enumerate(trial_orders):
            futures = {pool.submit(score, arm, repeat): arm for arm in trial_order}
            for future in as_completed(futures):
                arm = futures[future]
                try:
                    trial = future.result()
                except Exception as exc:
                    infrastructure_errors.append(dict(arm=arm, repeat=repeat,
                        phase='scoring_record', error_type=type(exc).__name__))
                    trial = dict(repeat=repeat, status='UNRESOLVED_EXECUTION', gold=None,
                                 error=type(exc).__name__)
                    # Do not start further calls on a branch with unknown execution.
                    candidates[arm] = None
                    unresolved.add(arm)
                result['arms'][arm]['trials'].append(trial)
                write_json(directory / 'result.json', result)
            # Exiting as_completed consumes every future before the next repeat.
    for arm, runner in runners.items():
        result['arms'][arm]['physical_cost'] = runner.metrics()['physical_total_cost']
    result['status'] = 'worker_error' if infrastructure_errors else 'completed'
    result['infrastructure_errors'] = infrastructure_errors
    write_json(directory / 'result.json', result)
    write_json(directory / 'status.json', dict(status=result['status']))
    return result


def reconcile(run, amendment, claims):
    """Called only after the original parent and every new case are terminal."""
    archive = amendment / 'original_parent_terminal'
    archive.mkdir(exist_ok=False)
    for name in ('results.json', 'summary.json', 'status.json'):
        if (run / name).exists():
            shutil.copy2(run / name, archive / name)
    previous = {r['case_id']: r for r in read(archive / 'results.json')} if (archive / 'results.json').exists() else {}
    plan = read(run / 'manifest.json')
    claimed = {c['case_id'] for c in claims if c['claimed']}
    results, handoffs = [], []
    for cid in plan['case_ids']:
        path = run / cid / 'result.json'
        row = read(path) if path.exists() else deepcopy(previous.get(cid, dict(case_id=cid, status='missing', arms={})))
        old = previous.get(cid, {})
        expected_handoff = cid in claimed and old.get('status') == 'worker_error' and old.get('error_type') == 'FileExistsError'
        if expected_handoff:
            handoffs.append(dict(case_id=cid, original_parent_error='FileExistsError',
                classification='exclusive_claim_scheduling_handoff', model_error=False))
        elif old.get('status') == 'worker_error':
            row.update(status='worker_error', original_parent_error_type=old.get('error_type'))
        terminal_path = amendment / (cid + '.terminal.json')
        if cid in claimed and terminal_path.exists():
            terminal = read(terminal_path)
            if terminal.get('status') != 'completed':
                row.update(status='worker_error',
                           parallel_worker_error_type=terminal.get('error_type', terminal.get('status')))
        results.append(row)
    summary = summarize(results, plan['case_ids'])
    summary['scheduling_amendment'] = dict(path=str(amendment), handoffs=handoffs,
        note='Only exclusive-claim FileExistsError is reclassified; candidate/Target errors and missing results remain.')
    write_json(run / 'results.json', results)
    write_json(run / 'summary.json', summary)
    incomplete = any(r.get('status') != 'completed' for r in results) or any(a['missing_slots'] for a in summary['arms'].values())
    write_json(run / 'status.json', dict(status='completed_with_errors' if incomplete else 'completed',
        completed_at=now(), scheduling_amendment=str(amendment), original_parent_terminal_archived=True))
    write_json(amendment / 'reconciliation.json', dict(completed_at=now(), handoffs=handoffs,
        preserved_case_files=True, original_root_archive=str(archive)))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--parent-pid', required=True, type=int)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    run = Path(args.run).resolve()
    plan = read(run / 'manifest.json')
    require(plan['kind'] == 'LIVE_FIXED_H1_REVISE_ABLATION' and tuple(plan['arms']) == ARMS
            and plan['repetitions'] == 2, 'Unexpected original protocol')
    verify_plan(plan)
    parent = validate_parent(args.parent_pid, run)
    workers = pool_workers(args.parent_pid)
    available = [cid for cid in CLAIM_CASES if not (run / cid).exists() and not (run / (cid + '.log')).exists()]
    amendment = run / AMENDMENT_DIR
    require(not amendment.exists(), 'A scheduling amendment already exists; do not resume')
    if args.dry_run:
        print(json.dumps(dict(status='offline_valid', api_calls=0, parent_pid=args.parent_pid,
            claimable_cases=available, original_budget=plan['limits'],
            max_new_parallel_requests=8, max_combined_parallel_requests=11)))
        return 0
    configure_credentials()
    amendment.mkdir(exist_ok=False)
    coordination_id = uuid.uuid4().hex
    claims = [claim_case(run, cid, coordination_id) for cid in CLAIM_CASES]
    manifest = dict(schema_version=1, created_at=now(), coordinator_pid=os.getpid(),
        coordination_id=coordination_id, user_instruction=USER_INSTRUCTION,
        original_parent=parent, original_workers=workers,
        original_manifest_sha256=hashlib.sha256((run / 'manifest.json').read_bytes()).hexdigest(),
        original_schedule=dict(case_workers=3, arm_generation='sequential', scoring='sequential'),
        new_schedule=dict(only_claimed_cases=claims, case_workers=2, arm_workers_per_case=4,
            generation_barrier=True, repeat_barrier=True, independent_sessions=True,
            no_same_arm_concurrency=True, original_submission_orders_retained=True),
        unchanged_budget=plan['limits'], preflight_repeated=False,
        original_manifest_unchanged=True, source_and_model_parameters_unchanged=True,
        interpretation='Scheduling amendment changes concurrent load and timing; report it with results.')
    write_json(amendment / 'manifest.json', manifest)
    (amendment / 'manifest.json').chmod(0o444)
    owned = [c['case_id'] for c in claims if c['claimed']]
    write_json(amendment / 'status.json', dict(status='running' if owned else 'no_claims', claims=claims))
    if not owned:
        return 0
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(execute_parallel_case, plan, cid, run, coordination_id): cid for cid in owned}
            for future in as_completed(futures):
                cid = futures[future]
                try:
                    result = future.result()
                    terminal = dict(case_id=cid, status=result['status'])
                except Exception as exc:
                    terminal = dict(case_id=cid, status='worker_error', error_type=type(exc).__name__)
                    write_json(run / cid / 'status.json', terminal)
                write_json(amendment / (cid + '.terminal.json'), terminal)
                print(json.dumps(terminal), flush=True)
        write_json(amendment / 'status.json', dict(status='waiting_original_parent', claims=claims))
        while any(parent_active(process) for process in [parent, *workers]):
            time.sleep(10)
        reconcile(run, amendment, claims)
        write_json(amendment / 'status.json', dict(status='completed', completed_at=now(), claims=claims))
        print(json.dumps(dict(status='reconciled', run=str(run))), flush=True)
        return 0
    except BaseException as exc:
        write_json(amendment / 'status.json', dict(status='interrupted', error_type=type(exc).__name__,
            recovery='Inspect durable case events; no automatic restart or resampling.'))
        print(json.dumps(dict(status='interrupted', error_type=type(exc).__name__)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
