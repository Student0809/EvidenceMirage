"""Five preselected native cases, original Controller, shared-H1 evidence ablation.

Independent cases run in separate processes. Each uses the unchanged common
Runner for both policies. Credentials stay in the process environment only.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.engine.config import without_source_hashes, check_frozen, digest
from src.engine.storage import RunStore, write_json


def now():
    return datetime.now(timezone.utc).isoformat()


def configure_credentials():
    from dotenv import dotenv_values
    original = dotenv_values(ROOT.parent / 'PIMiner/.env', interpolate=False)
    target = dotenv_values(ROOT.parent / 'MiniPIM/.env', interpolate=False)
    target_key = (os.environ.get('OPENAI_API_KEY') or os.environ.get('PIMINER_TARGET_OPENAI_API_KEY')
                  or original.get('OPENAI_API_KEY') or original.get('PIMINER_TARGET_OPENAI_API_KEY')
                  or target.get('PIMINER_TARGET_OPENAI_API_KEY') or target.get('OPENAI_API_KEY'))
    attacker_key = os.environ.get('DEEPSEEK_API_KEY') or original.get('DEEPSEEK_API_KEY')
    if not target_key or not attacker_key:
        raise RuntimeError('Configured Target or Attacker credential is missing')
    os.environ['OPENAI_API_KEY'] = target_key
    os.environ['OPENAI_BASE_URL'] = 'https://api2.aigcbest.top/v1'
    os.environ['ANTHROPIC_AUTH_TOKEN'] = attacker_key
    for key in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):
        os.environ.pop(key, None)
    os.environ.update(ANTHROPIC_BASE_URL='https://api.deepseek.com/anthropic',
        ANTHROPIC_MODEL='deepseek-flash[1m]', ANTHROPIC_DEFAULT_OPUS_MODEL='deepseek-flash[1m]',
        ANTHROPIC_DEFAULT_SONNET_MODEL='deepseek-flash[1m]', ANTHROPIC_DEFAULT_HAIKU_MODEL='deepseek-flash',
        CLAUDE_CODE_SUBAGENT_MODEL='deepseek-flash', CLAUDE_CODE_EFFORT_LEVEL='max',
        CLAUDE_CODE_AUTO_COMPACT_WINDOW='786432')


def run_case(frozen, case_id, output):
    # Recheck frozen data/resource bytes in every worker before any case API call.
    manifest = without_source_hashes(json.loads(Path(frozen).read_text()))
    cases = check_frozen(manifest)
    case = next(c for c in cases if c['case_id'] == case_id)
    config = manifest['config']
    with (Path(output).parent / (case_id + '.log')).open('x') as log, redirect_stdout(log), redirect_stderr(log):
        from main import adapters
        from src.engine.runner import Runner
        from src.evidence.observer import FixedObserver
        from src.policies.registry import build_policies
        target, attacker = adapters(config)
        child_manifest = dict(manifest, cases=[case], case_ids=[case_id],
            created_at=now(), parent_run=str(Path(output).parent),
            independence='previously_exposed_development_pilot',
            shared_h1=True, learned_controller_enabled=False)
        store = RunStore(output, child_manifest)
        runner = Runner(target, attacker, FixedObserver(**config['observation']),
            build_policies(config['policies']), store,
            post_failure_budget=config['post_failure_target_budget'], seed=config['seed'])
        metrics = runner.run([case])
        return dict(case_id=case_id, status='completed', outcomes=runner.outcomes,
                    target_executions=metrics['physical_total_cost']['target_executions'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', type=int, default=5)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.workers <= 5:
        raise ValueError('Workers must be between 1 and 5')
    frozen = Path(args.frozen).resolve()
    output = Path(args.output).resolve()
    manifest = without_source_hashes(json.loads(frozen.read_text()))
    cases = check_frozen(manifest)
    config = manifest['config']
    if ([c['case_id'] for c in cases] != ['c000', 'c001', 'c004', 'c005', 'c006']
            or set(config['policies']) != {'stage_aware', 'stage_aware_no_evidence'}
            or config['post_failure_target_budget'] != 3 or config['backend'] != 'agentdojo'
            or not config.get('require_observable_trigger')):
        raise ValueError('Pilot must match its preregistered cases and two-arm protocol')
    from src.adapters.agentdojo import validate_native_cases
    native = validate_native_cases(cases, config['target']['benchmark_version'])
    if output.exists():
        raise FileExistsError('Output already exists; never silently resume or resample')
    if args.dry_run:
        print(json.dumps(dict(status='offline_valid', cases=[c['case_id'] for c in cases],
            native=native, learned_controller_enabled=False, max_target_executions=35,
            max_router_calls=5, max_payload_generation_calls=35,
            preflight_calls=3, api_calls=0, workers=args.workers)))
        return
    configure_credentials()
    launch = dict(status='preflight', started_at=now(), pid=os.getpid(), cases=cases,
        **manifest, workers=args.workers, shared_h1=True, independent_heldout=False,
        learned_controller_enabled=False,
        target_endpoint=os.environ['OPENAI_BASE_URL'], attacker_endpoint=os.environ['ANTHROPIC_BASE_URL'])
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'manifest.json', launch)
    write_json(output / 'status.json', dict(status='preflight', started_at=now()))
    try:
        from main import adapters
        target, attacker = adapters(config)
        preflight = dict(native=native, target=target.preflight(), attacker=attacker.preflight())
        write_json(output / 'preflight.json', preflight)
        del target, attacker
        results = []
        write_json(output / 'status.json', dict(status='running', started_at=launch['started_at']))
        print('Preflight passed; launching five fixed cases with the original Controller.', flush=True)
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context('spawn')) as pool:
            futures = {pool.submit(run_case, str(frozen), c['case_id'], str(output / c['case_id'])): c['case_id'] for c in cases}
            for future in as_completed(futures):
                cid = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = dict(case_id=cid, status='worker_error', error_type=type(exc).__name__)
                results.append(result)
                write_json(output / 'worker_results.json', sorted(results, key=lambda r:r['case_id']))
                print(json.dumps(result, ensure_ascii=False), flush=True)
        write_json(output / 'status.json', dict(status='completed' if all(r['status']=='completed' for r in results) else 'completed_with_errors',
            completed_at=now(), selected_cases=len(cases), completed_workers=sum(r['status']=='completed' for r in results)))
    except BaseException as exc:
        write_json(output / 'status.json', dict(status='interrupted', error_type=type(exc).__name__,
            completed_at=now(), recovery='Inspect durable case logs and events. Do not silently repeat started executions.'))
        raise


if __name__ == '__main__':
    main()
