"""Explicitly continue the audited pre-request NFS failure without repeating Target calls."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from run_evidence_parallel import validate_protocol, initialization_metadata
from run_observable_pilot5_20261029 import configure_credentials
from src.engine.prefix_recovery import RecoveryPrefix, PrefixRecoveryRunner
from src.engine.storage import RunStore, write_json
from src.engine.config import without_source_hashes, digest


def now():
    return datetime.now(timezone.utc).isoformat()


def recovery_initialization(config):
    return dict(initialization_metadata(config), fresh_initial=False, reused_initial=True,
        reused_initial_scope='completed_prefix_of_same_experiment',
        initial_execution_note='Continue the completed current-experiment prefix; no Target request is repeated and no Oct23 historical H1 is imported.')


def run_case(frozen, source, case_id, output):
    manifest = without_source_hashes(json.loads(Path(frozen).read_text()))
    cases, _ = validate_protocol(manifest)
    recovery = RecoveryPrefix(source, manifest)
    case = next(c for c in cases if c['case_id'] == case_id)
    config = manifest['config']
    with (Path(output).parent / (case_id + '.log')).open('x') as log, redirect_stdout(log), redirect_stderr(log):
        from main import adapters
        from src.evidence.observer import FixedObserver
        from src.policies.registry import build_policies
        target, attacker = adapters(config)
        child = dict(manifest, cases=[case], case_ids=[case_id], created_at=now(),
            parent_run=str(Path(output).parent), recovery=recovery.metadata,
            **recovery_initialization(config), independent_heldout=False,
            learned_controller_enabled=False, evaluation_scope=config.get('evaluation_scope'),
            total_target_budget=config['post_failure_target_budget'] + 1)
        store = RunStore(output, child)
        runner = PrefixRecoveryRunner(target, attacker, FixedObserver(**config['observation']),
            build_policies(config['policies']), store, recovery=recovery,
            post_failure_budget=config['post_failure_target_budget'], seed=config['seed'])
        metrics = runner.run([case])
        metrics.update(total_target_budget=config['post_failure_target_budget'] + 1,
            total_budget_definition='Previously completed current-experiment prefix plus newly started executions, at most five in total; never sum source and continuation as independent runs.',
            **recovery_initialization(config), primary_denominator='All assigned cases; errors remain missing.')
        write_json(store.path / 'metrics.json', metrics)
        return dict(case_id=case_id, status='completed', outcomes=runner.outcomes,
            target_executions=metrics['physical_total_cost']['target_executions'],
            recovered_prefix_target_executions=metrics['recovered_prefix_target_executions'],
            new_target_executions=metrics['new_target_executions'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen', 'source-run', 'output', 'report-prefix', 'coverage'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--workers', type=int, default=16)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if not 1 <= args.workers <= 32:
        raise ValueError('Invalid worker count')
    frozen, source, output, prefix = [Path(value).resolve() for value in
        (args.frozen, args.source_run, args.output, args.report_prefix)]
    if output.exists() or prefix.with_suffix('.supervisor.json').exists():
        raise FileExistsError('Recovery output exists; never automatically restart it')
    manifest = without_source_hashes(json.loads(frozen.read_text()))
    cases, _ = validate_protocol(manifest)
    recovery = RecoveryPrefix(source, manifest)
    config = manifest['config']
    from src.adapters.agentdojo import validate_native_cases
    native = validate_native_cases(cases, config['target']['benchmark_version'])
    workers = min(args.workers, len(cases))
    carried = sum(len(recovery.for_case(case)) for case in cases)
    total_limit = len(cases) * (config['post_failure_target_budget'] + 1)
    limits = dict(total_target_budget_per_case=config['post_failure_target_budget'] + 1,
        recovered_prefix_target_executions=carried, max_total_target_executions=total_limit,
        max_new_target_executions=total_limit-carried,
        max_router_calls=0, max_new_generation_calls=total_limit-carried,
        new_preflight_model_calls=0, source_preflight_included=True)
    if args.dry_run:
        print(json.dumps(dict(status='offline_valid', api_calls=0, cases=len(cases),
            workers=workers, native=native, limits=limits, source_run=str(source))))
        return 0
    configure_credentials()
    source_manifest = json.loads((source / 'manifest.json').read_text())
    if (os.environ['OPENAI_BASE_URL'] != source_manifest.get('target_endpoint')
            or os.environ['ANTHROPIC_BASE_URL'] != source_manifest.get('attacker_endpoint')):
        raise ValueError('Recovery endpoints differ from the completed prefix')
    started = now()
    output.mkdir(parents=True, exist_ok=False)
    launch = dict(manifest, cases=cases, workers=workers, created_at=started,
        recovery=recovery.metadata, **recovery_initialization(config),
        independent_heldout=False, learned_controller_enabled=False,
        evaluation_scope=config.get('evaluation_scope'), limits=limits,
        total_target_budget=config['post_failure_target_budget']+1,
        pid=os.getpid(),
        target_endpoint=os.environ['OPENAI_BASE_URL'], attacker_endpoint=os.environ['ANTHROPIC_BASE_URL'],
        source_preflight_counting_note='All three original preflight requests remain part of this experiment; no additional connectivity requests are made.',
        randomness_note='The explicit recovery pause changes scheduling; provider randomness is not controlled by the harness seed.')
    write_json(output/'manifest.json', launch)
    write_json(output/'status.json', dict(status='preflight', started_at=started))
    run_code, summary_code, results = 0, None, []
    try:
        # Exercise the actual project mount, which differs from /tmp on this host.
        from src.attacker.experience import RollingExperience
        _, filesystem = RollingExperience(output/'runtime_validation'/'memory').snapshot('c999')
        write_json(output/'preflight.json', dict(status='passed', native=native,
            source_run=str(source), source_preflight=recovery.metadata['source_preflight'],
            new_api_calls=0, filesystem_lock='passed', filesystem_snapshot=filesystem))
        write_json(output/'status.json', dict(status='running', started_at=started))
        print(json.dumps(dict(status='recovery_started', workers=workers, limits=limits)), flush=True)
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn')) as pool:
            futures = {pool.submit(run_case, str(frozen), str(source), c['case_id'],
                                  str(output/c['case_id'])): c['case_id'] for c in cases}
            for future in as_completed(futures):
                cid = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = dict(case_id=cid, status='worker_error', error_type=type(exc).__name__)
                results.append(result)
                write_json(output/'worker_results.json', sorted(results, key=lambda row: row['case_id']))
                print(json.dumps(result), flush=True)
        run_code = 0 if all(r['status']=='completed' for r in results) else 1
        write_json(output/'status.json', dict(status='completed' if run_code==0 else 'completed_with_errors',
            completed_at=now(), selected_cases=len(cases), completed_workers=sum(r['status']=='completed' for r in results)))
    except BaseException as exc:
        run_code = 1
        write_json(output/'status.json', dict(status='interrupted', error_type=type(exc).__name__, completed_at=now()))
        raise
    finally:
        prefix.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(ROOT/'scripts/summarize_full_evidence.py'),
            '--run', str(output), '--output-prefix', str(prefix),
            '--coverage', str(Path(args.coverage).resolve()), '--verify-completed']
        summary_code = subprocess.run(command, cwd=ROOT).returncode
        write_json(prefix.with_suffix('.supervisor.json'), dict(started_at=started, completed_at=now(),
            run_returncode=run_code, summary_returncode=summary_code, run=str(output),
            source_run=str(source), report_prefix=str(prefix)))
    return run_code or summary_code or 0


if __name__ == '__main__':
    raise SystemExit(main())
