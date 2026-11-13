"""Task-configured live sampling; frozen V5 rows select tasks, not live mismatches."""
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path

from .data import ROOT, digest, write_json
from .online import configure, selection, stamp
from .sampling import TARGET_BUDGET, run_sampling_branch
from ..engine.storage import plain
from ..evidence.task_sampling import PROTOCOL, load_profiles, validate_capabilities
from ..feedback.ablation import ABLATION_PROTOCOL, AssessmentModules


def ablation_metadata(modules, mode, visibility):
    return dict(modules.metadata(), feedback_view='task_feedback',
                base_feedback_view=modules.config.feedback_view,
                view_override_protocol=PROTOCOL, report_requirement=mode,
                feedback_visibility=visibility,
                initial_payload='attacker_generated_fresh_p0')


def validate_sampling(prepared, selection_path, profiles_path, mode, repeats=1):
    """No credentials, model clients, or target executions in this check."""
    from .tasks import resolve, identify
    if mode not in ('probe', 'no_probe') or type(repeats) is not int or repeats < 1:
        raise ValueError('Invalid sampling mode or repeat count')
    versions = {name: importlib.metadata.version(name)
                for name in ('agentdojo', 'openai', 'pydantic')}
    if versions['agentdojo'] != '0.1.35' or versions['openai'] != '1.107.3':
        raise ValueError('Sampling requires agentdojo 0.1.35 and openai 1.107.3')
    profiles = load_profiles(profiles_path)
    _, instances = selection(prepared, selection_path)
    checked = []
    for public, label in instances:
        sid, case = label['seed_id'], public['case']
        profile = profiles[sid]
        if identify(case) != sid or case['suite_name'] != profile['suite']:
            raise ValueError('Selected task and profile identity differ: ' + sid)
        suite, user, injection = resolve(case)
        if case['user_task_prompt'] != user.PROMPT or case['injection_task_goal'] != injection.GOAL:
            raise ValueError('Public task differs from resolved task: ' + sid)
        if not case['injection_candidates'] or set(case['injection_candidates']) - set(suite.get_injection_vector_defaults()):
            raise ValueError('Unknown or empty injection vectors: ' + sid)
        env = suite.load_and_inject_default_environment({v: '' for v in case['injection_candidates']})
        env = user.init_environment(env)
        validate_capabilities(profile, {tool.name for tool in suite.tools}, plain(env))
        checked.append(dict(instance_id=label['instance_id'], seed_id=sid,
                            suite=case['suite_name'], route=profile['route']))
    return dict(status='offline_valid', protocol=PROTOCOL, mode=mode,
                instances=[row['instance_id'] for row in checked], checked=checked,
                profiles=profiles, repeats=repeats, api_calls=0, environment=versions,
                target_budget_per_branch=TARGET_BUDGET,
                max_target_dispatches=TARGET_BUDGET * len(instances) * repeats,
                prepared_manifest_sha256=digest(Path(prepared) / 'manifest.json'),
                resumable=False)


def prepare_campaign(args):
    """Validate and freeze reviewable jobs. This function never calls a model."""
    if args.ablation not in ('G10', 'G11'):
        raise ValueError('Use G10/G11 for history; feedback visibility is a separate switch')
    mode = args.mode
    visibility = args.feedback_visibility or ('visible' if mode == 'probe' else 'hidden')
    modules = AssessmentModules(args.ablation)
    validation = validate_sampling(args.prepared, args.selection, args.profiles, mode, args.repeats)
    profiles = validation['profiles']
    target = dict(model='gpt-5-nano', benchmark_version='v1.2.2',
                  reasoning_effort=None, request_timeout=120, max_tool_calls=30)
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError('Output exists; no implicit resume or resampling')
    if args.dry_run:
        return dict(validation, ablation=ablation_metadata(modules, mode, visibility), target=target), []
    _, instances = selection(args.prepared, args.selection)
    if [label['instance_id'] for _, label in instances] != validation['instances']:
        raise ValueError('Selection changed after validation')
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'selection.json', json.loads(Path(args.selection).read_text()))
    write_json(output / 'profiles.json', dict(protocol=PROTOCOL, profiles=profiles))
    mapping, jobs = [], []
    for repeat in range(1, args.repeats + 1):
        for public, label in instances:
            sid = label['seed_id']
            profile = deepcopy(profiles[sid])
            folder = output / 'branches' / f'b{len(jobs):03d}'
            # Frozen mismatched responses, P0 and annotations are NOT live inputs.
            model_input = {key: deepcopy(public[key]) for key in ('case', 'strategy_document')}
            grants_sha = hashlib.sha256(json.dumps(profile['resource_grants'], sort_keys=True).encode()).hexdigest()
            job = dict(protocol=PROTOCOL, mode=mode, input=model_input, seed=sid,
                       suite=profile['suite'], route=profile['route'], repeat=repeat,
                       profile=profile, resource_grants_sha256=grants_sha,
                       model='deepseek-flash[1m]', effort='max', timeout=900,
                       view='task_feedback', feedback_visibility=visibility,
                       ablation=args.ablation, ablation_protocol=ABLATION_PROTOCOL,
                       target_config=dict(target, task_feedback=deepcopy(profile)), budget=TARGET_BUDGET)
            write_json(folder / 'job_public.json', job)
            mapping.append(dict(label, branch=folder.name, seed=sid, suite=profile['suite'],
                                route=profile['route'], repeat=repeat))
            jobs.append(folder)
    write_json(output / 'selection_private.json', mapping)
    sources = list((ROOT / 'src').rglob('*.py'))
    manifest = dict(started_at=stamp(), result_kind='LIVE_TASK_FEEDBACK_SAMPLING',
                    protocol=PROTOCOL, mode=mode, view='task_feedback', method='piminer',
                    feedback_visibility=visibility, profiles=profiles,
                    profiles_sha256=digest(output / 'profiles.json'),
                    model='deepseek-flash[1m]', effort='max', target=target,
                    workers=args.workers, repeats=args.repeats,
                    target_budget_per_branch=TARGET_BUDGET,
                    max_target_dispatches=validation['max_target_dispatches'], resumable=False,
                    initial_payload='attacker_generated_fresh_p0',
                    initial_feedback='none; fresh P0 execution before assessment',
                    selection_semantics='task selection only; live execution does not preserve frozen M2 mismatch',
                    ablation=ablation_metadata(modules, mode, visibility), validation=validation,
                    input_sha256={str(path): digest(path) for path in (
                        Path(args.prepared) / 'manifest.json', Path(args.prepared) / 'model_inputs.jsonl',
                        Path(args.prepared) / 'scoring_private.jsonl', Path(args.selection), Path(args.profiles))},
                    code_sha256={str(path.relative_to(ROOT)): digest(path) for path in sources})
    write_json(output / 'manifest.json', manifest)
    write_json(output / 'status.json', dict(status='prepared', completed=0, total=len(jobs)))
    return manifest, jobs


def campaign(args):
    manifest, jobs = prepare_campaign(args)
    if args.dry_run:
        print(json.dumps(manifest))
        return
    from ..adapters.agentdojo import AgentDojoAdapter
    from ..attacker.piminer import PIMinerAdapter
    output = Path(args.output).resolve()
    configure()
    os.environ.update(PIMINER_CALL_LOG=str(output / 'preflight_calls.jsonl'),
                      PIMINER_STOP_FILE=str(output / 'STOP.json'), PIMINER_RUN_ID=output.name)
    write_json(output / 'status.json', dict(status='preflight', completed=0, total=len(jobs)))
    try:
        preflight = dict(target=AgentDojoAdapter(**manifest['target']).preflight(),
                         attacker=PIMinerAdapter(ROOT, manifest['model'], manifest['effort'], 900).preflight())
        write_json(output / 'preflight.json', preflight)
    except Exception as exc:
        write_json(output / 'status.json', dict(status='preflight_error', error_type=type(exc).__name__,
                                              completed=0, total=len(jobs), updated_at=stamp()))
        raise
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_sampling_branch, str(path)): path.name for path in jobs}
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                row = dict(branch=futures[future], status='worker_error', error_type=type(exc).__name__)
            results.append(row)
            write_json(output / 'progress.json', sorted(results, key=lambda item: item['branch']))
            write_json(output / 'status.json', dict(status='running', completed=len(results),
                                                  total=len(jobs), updated_at=stamp(), results=results))
            print(json.dumps(row), flush=True)
    write_json(output / 'status.json', dict(status='predictions_frozen', completed=len(results),
                                          total=len(jobs), updated_at=stamp(), results=results))
