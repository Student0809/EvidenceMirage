"""Run one Evidence policy on development or audited benchmark cases in parallel.

Every case starts with a fresh native execution. Original mode generates P0;
aligned mode imports the historical route/P0 without historical Target feedback.
A frozen configuration controls per-case budgets; only independent cases run in
parallel. Existing run directories are never resumed or overwritten.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stdout, redirect_stderr
import json
import hashlib
import multiprocessing
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from run_observable_pilot5_20261029 import configure_credentials, now
from src.engine.config import check_frozen, data_fingerprints, without_source_hashes
from src.engine.storage import RunStore, write_json
from asr_holdout_guard import heldout_evidence, verify_child_claim, evaluation_metadata


def load_data_manifest(path):
    """Read legacy locks without copying obsolete source fingerprints into a new run."""
    return without_source_hashes(json.loads(Path(path).read_text()))


def validate_protocol(manifest, reference=None):
    cases = check_frozen(manifest)
    config = manifest['config']
    snapshot_manifest = config.get('experiment', {}).get('source_snapshot_manifest')
    if snapshot_manifest:
        snapshot = json.loads((ROOT / snapshot_manifest).read_text())
        if Path(snapshot['snapshot']).resolve() != ROOT:
            raise ValueError('Running outside frozen source tree')
        for name, expected in snapshot['files'].items():
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
                raise ValueError('Frozen source changed: ' + name)
    selection = config.get('experiment', {}).get('assigned_case_ids')
    if selection is not None:
        known = {c['case_id']: c for c in cases}
        if (not isinstance(selection, list) or not selection
                or any(not isinstance(cid, str) or cid not in known for cid in selection)
                or len(set(selection)) != len(selection)):
            raise ValueError('Invalid frozen assigned_case_ids')
        cases = [known[cid] for cid in selection]
    observable_protocol = (config.get('require_observable_trigger') is True
        or (config['split'] == 'benchmark'
            and config.get('allow_unobservable_trigger') is True
            and config.get('require_observable_trigger') is False))
    if (config['backend'] != 'agentdojo' or config['split'] not in ('dev', 'benchmark', 'test')
            or set(config['policies']) not in ({'stage_aware'}, {'stage_aware_p0_confirm'})
            or config['attacker'].get('mode', 'original') not in ('original', 'piminer_aligned')
            or not observable_protocol):
        raise ValueError('This launcher requires original or aligned Attacker + single stage_aware or stage_aware_p0_confirm on observable development, test, or audited benchmark cases; unobservable benchmark cases require an explicit audited opt-in')
    heldout_evidence(ROOT, manifest, [c['case_id'] for c in cases])
    comparison = None
    if reference is not None:
        ref = json.loads((Path(reference) / 'manifest.json').read_text())
        if cases != ref['cases']:
            raise ValueError('Reference case snapshots differ')
        keys = ('target', 'attacker', 'observation', 'seed', 'dataset', 'goal_specs')
        if any(config[key] != ref['config'][key] for key in keys):
            raise ValueError('Reference runtime configuration differs')
        policy_id = next(iter(config['policies']))
        reference_policies = ref['config']['policies']
        if policy_id not in reference_policies or config['policies'][policy_id] != reference_policies[policy_id]:
            raise ValueError('Reference Controller differs')
        previous_inputs = data_fingerprints(ref['inputs_sha256'])
        current_inputs = data_fingerprints(manifest['inputs_sha256'])
        changed = [p for p, sha in previous_inputs.items() if current_inputs.get(p) != sha]
        comparison = dict(reference_run=str(Path(reference).resolve()), case_snapshots_equal=True,
            runtime_settings_equal=True, controller_settings_equal=True,
            policy_id=policy_id, reference_policy_id=policy_id,
            controller_comparison_scope='Selected same-named policy; other reference arms are excluded.',
            changed_previous_input_paths=changed,
            reference_post_failure_budget=ref['config']['post_failure_target_budget'],
            current_post_failure_budget=config['post_failure_target_budget'],
            comparison_rule=('Compare at current total budget; truncate older longer runs. '
                + ('Historical route/P0 are shared; Target H1 is freshly executed in each run.'
                   if config['attacker'].get('mode') == 'piminer_aligned'
                   else 'No shared P0/H1 across runs.')))
    return cases, comparison


def initialization_metadata(config):
    reused = config['attacker'].get('mode', 'original') == 'piminer_aligned'
    metadata = dict(fresh_initial=True, reused_initial=False, reused_route=reused,
                    reused_payload=reused,
                    initial_execution_note='P0 is executed in a fresh Target environment; no historical H1 is reused.')
    if reused and config.get('experiment', {}).get('initial_payload_source') == 'piminer_last_evaluated_attempt':
        metadata.update(initial_payload_source='piminer_last_evaluated_attempt',
            initial_analysis_policy='empty_exclude_historical_revision_reasoning',
            initial_execution_note=('The last evaluated original PIMiner payload is executed in a fresh Target environment. '
                                    'P0 denotes this run\'s initial slot; historical H1, feedback and revision analysis are not reused.'))
    return metadata


def execution_limits(config, case_count):
    aligned = config['attacker'].get('mode', 'original') == 'piminer_aligned'
    budget = config['post_failure_target_budget']
    mixed = config['attacker'].get('false_revision_mode') == 'grounded'
    limits = dict(total_target_budget_per_case=budget + 1,
        max_target_executions=case_count * (budget + 1),
        max_router_calls=0 if aligned else case_count,
        max_payload_generation_calls=case_count * (budget if aligned else budget + 1) * (2 if mixed else 1),
        preflight_model_calls=4 if mixed else 3)
    if mixed:
        from src.attacker.piminer import REVISION_PROTOCOL
        limits.update(max_revision_requests=case_count * budget,
            grounded_revision_protocol=REVISION_PROTOCOL,
            max_planner_calls=case_count * budget * 4,
            max_semantic_review_calls=case_count * budget * 2,
            max_attacker_model_calls=case_count * budget * 8,
            generator_counting_note=('F: planner plus at most one format correction, generator '
                'plus at most one shared repair, and up to two advisory semantic reviews; '
                'a plan conflict permits up to two additional plan-correction calls; '
                'U: one original black-box generation. Target budget is shared.'))
    return limits


def run_case(frozen, case_id, output):
    manifest = load_data_manifest(frozen)
    cases, _ = validate_protocol(manifest)
    case = next(c for c in cases if c['case_id'] == case_id)
    config = manifest['config']
    claim = verify_child_claim(ROOT, frozen, manifest, [c['case_id'] for c in cases], Path(output).parent)
    with (Path(output).parent / (case_id + '.log')).open('x') as log, redirect_stdout(log), redirect_stderr(log):
        from main import adapters
        from src.engine.runner import Runner
        from src.evidence.observer import FixedObserver
        from src.policies.registry import build_policies
        target, attacker = adapters(config)
        child = dict(manifest, cases=[case], case_ids=[case_id], created_at=now(),
            parent_run=str(Path(output).parent), policy_id=next(iter(config['policies'])),
            **initialization_metadata(config),
            **evaluation_metadata(config, claim), learned_controller_enabled=False,
            total_target_budget=config['post_failure_target_budget'] + 1)
        store = RunStore(output, child)
        runner = Runner(target, attacker, FixedObserver(**config['observation']),
            build_policies(config['policies']), store,
            post_failure_budget=config['post_failure_target_budget'], seed=config['seed'])
        metrics = runner.run([case])
        metrics.update(total_target_budget=config['post_failure_target_budget'] + 1,
            total_budget_definition='One fresh initial execution plus at most the configured post-failure executions.',
            **initialization_metadata(config), primary_denominator='All assigned cases; errors remain missing.')
        write_json(store.path / 'metrics.json', metrics)
        return dict(case_id=case_id, status='completed', outcomes=runner.outcomes,
                    target_executions=metrics['physical_total_cost']['target_executions'])


def launch_workers(frozen, cases, output, workers):
    """Each worker constructs its own adapters, environment, history and RunStore."""
    results = []
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn')) as pool:
        futures = {pool.submit(run_case, str(frozen), c['case_id'], str(output / c['case_id'])): c['case_id'] for c in cases}
        for future in as_completed(futures):
            cid = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = dict(case_id=cid, status='worker_error', error_type=type(exc).__name__)
            results.append(result)
            write_json(output / 'worker_results.json', sorted(results, key=lambda r: r['case_id']))
            print(json.dumps(result, ensure_ascii=False), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--reference-run')
    parser.add_argument('--workers', type=int, default=None,
        help='Case workers; defaults to experiment.case_parallelism, otherwise 5')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    frozen, output = Path(args.frozen).resolve(), Path(args.output).resolve()
    manifest = load_data_manifest(frozen)
    cases, comparison = validate_protocol(manifest, args.reference_run)
    config = manifest['config']
    requested_workers = (args.workers if args.workers is not None else
                         config.get('experiment', {}).get('case_parallelism', 5))
    if type(requested_workers) is not int or not 1 <= requested_workers <= 32:
        raise ValueError('Workers must be between 1 and 32')
    workers = min(requested_workers, len(cases))
    from src.adapters.agentdojo import validate_native_cases
    native = validate_native_cases(cases, config['target']['benchmark_version'])
    if output.exists():
        raise FileExistsError('Output exists; never silently resume or resample')
    limits = execution_limits(config, len(cases))
    if args.dry_run:
        print(json.dumps(dict(status='offline_valid', api_calls=0, case_ids=[c['case_id'] for c in cases],
            workers=workers, policy_id=next(iter(config['policies'])),
            **initialization_metadata(config), native=native, limits=limits, reference_comparison=comparison)))
        return
    if config['split'] == 'test':
        raise ValueError('Test execution requires launch_asr_interleaved.py and its one-time cohort claim')
    configure_credentials()
    launch = dict(manifest, cases=cases, case_ids=[c['case_id'] for c in cases], status='preflight', created_at=now(), pid=os.getpid(),
        workers=workers, policy_id=next(iter(config['policies'])), **initialization_metadata(config),
        independent_heldout=False, learned_controller_enabled=False,
        evaluation_scope=config.get('evaluation_scope', 'development'),
        total_target_budget=config['post_failure_target_budget']+1, limits=limits,
        reference_comparison=comparison,
        target_endpoint=os.environ['OPENAI_BASE_URL'], attacker_endpoint=os.environ['ANTHROPIC_BASE_URL'],
        randomness_note='The configured seed controls harness order, not external LLM randomness.')
    output.mkdir(parents=True, exist_ok=False)
    os.environ.setdefault('PIMINER_CALL_LOG', str(output / 'calls.jsonl'))
    os.environ.setdefault('PIMINER_STOP_FILE', str(output / 'STOP.json'))
    from src.attacker.accounting_proxy import AccountingProxy
    proxy = AccountingProxy(os.environ['ANTHROPIC_BASE_URL'])
    os.environ['ANTHROPIC_BASE_URL'] = proxy.start()
    os.environ['PIMINER_ACCOUNTING_PROXY'] = '1'
    write_json(output / 'manifest.json', launch)
    write_json(output / 'status.json', dict(status='preflight', started_at=launch['created_at']))
    try:
        from main import adapters
        target, attacker = adapters(config)
        write_json(output / 'preflight.json', dict(native=native, target=target.preflight(), attacker=attacker.preflight()))
        del target, attacker
        write_json(output / 'status.json', dict(status='running', started_at=launch['created_at']))
        print(json.dumps(dict(status='preflight_passed', workers=workers, case_ids=[c['case_id'] for c in cases], limits=limits)), flush=True)
        results = launch_workers(frozen, cases, output, workers)
        write_json(output / 'status.json', dict(status='completed' if all(r['status']=='completed' for r in results) else 'completed_with_errors',
            completed_at=now(), selected_cases=len(cases), completed_workers=sum(r['status']=='completed' for r in results)))
    except BaseException as exc:
        write_json(output / 'status.json', dict(status='interrupted', error_type=type(exc).__name__, completed_at=now(),
            recovery='Inspect durable events before any new run; do not silently repeat started executions.'))
        raise
    finally:
        proxy.close()


if __name__ == '__main__':
    main()
