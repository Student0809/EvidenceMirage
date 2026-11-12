"""Jailbreak-based active evidence sampling campaign (S01/G11 first).

The Attacker generates a sampling-aware P0 under the same public case and
frozen strategy document. The target-side receiver channel returns only the
minimal report actually delivered to the frozen mailbox. This entry is separate
from the legacy Mismatch campaign and is deliberately non-resumable.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess

from .data import ROOT, digest, write_json
from .online import PYTHON, configure, positive_workers, selection, stamp
from ..attacker.piminer import GenerationError, PIMinerAdapter
from ..engine.feedback_runner import FeedbackRunner
from ..engine.storage import plain
from ..evidence.probe_sampling import (
    PROTOCOL, ProbeChannel, SamplingContext, nonce, validate_channel_config,
    PAYLOAD_POLICY, validate_no_probe_payload,
)
from ..feedback.ablation import ABLATION_PROTOCOL, AssessmentModules
from ..feedback.schema import BranchResult, PROTOCOL as FEEDBACK_PROTOCOL, PublicAttempt

TARGET_BUDGET = 3  # Includes the initial P0 execution.
VIEWS = {'probe': 'receiver_report_only', 'no_probe': 'final_only'}
GROUPS = {'probe': 'G11', 'no_probe': 'G10'}


def _ablation(modules, mode):
    return dict(modules.metadata(),
                probe_payload_policy=PAYLOAD_POLICY if mode == 'probe' else 'disabled',
                base_feedback_view=modules.config.feedback_view,
                feedback_view=VIEWS[mode],
                view_override_protocol=PROTOCOL if mode == 'probe' else None,
                initial_payload=('attacker_generated_sampling_aware_p0' if mode == 'probe'
                                 else 'attacker_generated_no_probe_p0'))


def _case_pre(case):
    from .tasks import resolve
    suite, user, _ = resolve(case)
    env = suite.load_and_inject_default_environment(
        {vector: '' for vector in case['injection_candidates']})
    env = user.init_environment(env)
    return plain(env)


def validate_sampling(prepared, selection_path, channel_path, mode):
    if mode not in VIEWS:
        raise ValueError('Unknown sampling mode')
    prepared = Path(prepared)
    import importlib.metadata
    versions = {name: importlib.metadata.version(name)
                for name in ('agentdojo', 'openai', 'pydantic')}
    if versions['agentdojo'] != '0.1.35' or versions['openai'] != '1.107.3':
        raise ValueError('Sampling requires agentdojo 0.1.35 and openai 1.107.3')
    manifest = json.loads((prepared / 'manifest.json').read_text())
    for name, checksum in manifest['files'].items():
        if digest(prepared / name) != checksum:
            raise ValueError('Prepared inputs changed: ' + name)
    _, instances = selection(prepared, selection_path)
    if not instances:
        raise ValueError('Sampling selection is empty')
    channel = None
    if channel_path is not None:
        value = json.loads(Path(channel_path).read_text())
        if 'nonce' in value:
            raise ValueError('Frozen channel template must not carry a nonce')
        channel = validate_channel_config(value)
    if mode == 'probe' and channel is None:
        raise ValueError('Probe mode requires --channel')
    checked = []
    for public, label in instances:
        if label['seed_id'] != 'S01':
            raise ValueError('Sampling v1 is scoped to S01')
        case = public['case']
        if case['suite_name'] != 'workspace':
            raise ValueError('Sampling v1 requires the native workspace suite')
        if case['injection_task_id'] != 'injection_task_11':
            raise ValueError('Sampling v1 is scoped to S01 injection_task_11')
        pre = _case_pre(case)
        files = pre['cloud_drive']['files']
        ranked = sorted(files.items(), key=lambda item: item[1]['size'], reverse=True)[:5]
        if channel is not None:
            if {str(key) for key, _ in ranked} != {
                    obj['id'] for obj in channel['objects'] if obj['kind'] == 'file'}:
                raise ValueError('Probe file objects are not the S01 five largest files')
            probe = ProbeChannel(dict(channel, nonce='0' * 16), episode_id='validation',
                                 account_email=pre['inbox']['account_email'])
            probe.validate_pre(pre)
        checked.append(dict(instance_id=label['instance_id'], seed_id=label['seed_id'],
                            case_id=case.get('case_id', label['seed_id']),
                            pre_largest_files=sorted(str(key) for key, _ in ranked)))
    return dict(status='offline_valid', protocol=PROTOCOL, mode=mode,
                instances=[row['instance_id'] for row in checked], checked=checked,
                channel=channel, max_target_dispatches=TARGET_BUDGET * len(instances),
                target_budget_per_branch=TARGET_BUDGET, api_calls=0,
                prepared_manifest_sha256=digest(prepared / 'manifest.json'),
                environment=versions, resumable=False)


def _freeze(folder):
    write_json(folder / 'frozen.json', dict(status='frozen', finished_at=stamp(),
        files={str(path.relative_to(folder)): digest(path)
               for path in sorted(folder.rglob('*.json'))
               if path.name not in ('frozen.json', 'active.json')}))


def _probe_present(payload, context):
    if context is None:
        return False
    return bool(re.search(r'(?<![A-Za-z0-9])' + re.escape(context.nonce)
                          + r'(?![A-Za-z0-9])', payload.text))


def run_sampling_branch(branch_path):
    """One branch: generate P0, execute it, then run the autonomous feedback loop."""
    folder = Path(branch_path)
    job = json.loads((folder / 'job_public.json').read_text())
    if job.get('protocol') != PROTOCOL:
        raise ValueError('Sampling job protocol differs')
    if job.get('ablation_protocol') != ABLATION_PROTOCOL:
        raise ValueError('Legacy or missing ablation protocol; start a fresh sampling run')
    probe = job['mode'] == 'probe'
    if job['view'] != VIEWS[job['mode']] or job.get('ablation') != GROUPS[job['mode']]:
        raise ValueError('Sampling mode, view and base ablation conflict')
    if probe and not isinstance(job['target_config'].get('probe_sampling'), dict):
        raise ValueError('Probe mode requires a channel template')
    if not probe and job['target_config'].get('probe_sampling') is not None:
        raise ValueError('No-probe mode must not carry a probe channel')
    modules = AssessmentModules(job['ablation'])
    configure()
    os.environ.update(PIMINER_CALL_LOG=str(folder / 'provider_calls.jsonl'),
                      PIMINER_STOP_FILE=str(folder / 'STOP.json'), PIMINER_RUN_ID=folder.name)
    public, strategy = job['input']['case'], job['input']['strategy_document']
    adapter = PIMinerAdapter(ROOT, job['model'], job['effort'], job['timeout'],
                            ablation=modules.config, sampling_mode=job['mode'])
    transport = adapter.decide
    calls = []
    active = dict(stage='starting', updated_at=stamp())
    write_json(folder / 'active.json', active)

    def tracked(prompt, stage):
        index = len(calls)
        item = dict(stage=stage, status='running', started_at=stamp(), prompt=prompt)
        calls.append(item)
        active.update(stage=stage, updated_at=stamp())
        write_json(folder / 'active.json', active)
        write_json(folder / 'calls' / f'{index:03d}.json', item)
        before = len(getattr(transport, 'calls', []))
        try:
            output = transport(prompt, stage)
            item.update(status='completed', output=output)
            return output
        except Exception as exc:
            item.update(status='error', error_type=type(exc).__name__)
            raise
        finally:
            item['finished_at'] = stamp()
            if len(getattr(transport, 'calls', [])) > before:
                item['provider'] = transport.calls[-1]
            write_json(folder / 'calls' / f'{index:03d}.json', item)

    adapter.decide = tracked
    nonce_by_sha = {}
    template = job['target_config'].get('probe_sampling') if probe else None

    def context_for_payload():
        if template is None:
            return None
        return SamplingContext(template, nonce())

    original_revise = adapter.revise

    def sampling_revise(attempt, history, decision, *, feedback_view):
        context = context_for_payload()
        payload, record = original_revise(attempt, history, decision,
                                          feedback_view=feedback_view, sampling=context)
        if context is not None:
            # An identical payload keeps the nonce embedded in its bytes.
            context = nonce_by_sha.setdefault(payload.sha256, context)
            record['sampling'] = context.public()
        return payload, record

    adapter.revise = sampling_revise
    attempts = []
    write_json(folder / 'attempts_public.json', attempts)

    def persist(result):
        if result.ablation is not None:
            result.ablation = _ablation(modules, job['mode'])
        write_json(folder / 'branch.json', asdict(result))
        active.update(status=result.status, target_attempts=result.target_attempts,
                      updated_at=stamp())
        write_json(folder / 'active.json', active)

    def source(current, payload, ordinal):
        context = nonce_by_sha.get(payload.sha256) if probe else None
        if probe and context is None:
            raise ValueError('Probe payload lacks a sampling context')
        # Validate before creating an execution or spending a Target API call.
        if probe:
            context.validate_payload(payload.text)
        else:
            validate_no_probe_payload(payload.text)
        execution = folder / 'executions' / f'{ordinal:02d}'
        execution.mkdir(parents=True, exist_ok=False)
        target_config = deepcopy(job['target_config'])
        if probe:
            target_config['probe_sampling'] = dict(target_config['probe_sampling'],
                                                   nonce=context.nonce)
        request = dict(case=current.case, payload=asdict(payload),
                       target_config=target_config,
                       episode_id=f'{folder.name}/live/{ordinal}',
                       feedback_projection='probe_receiver' if probe else 'none',
                       final_projection='text_only')
        write_json(execution / 'request_public.json', request)
        active.update(stage='target_execution', updated_at=stamp())
        write_json(folder / 'active.json', active)
        with (execution / 'process.log').open('w') as log:
            subprocess.run([PYTHON, '-m', 'src.mismatch.online', 'target-job',
                            '--request', str(execution / 'request_public.json')], cwd=ROOT,
                           stdout=log, stderr=log, check=True, timeout=1800)
        response = json.loads((execution / 'feedback_public.json').read_text())
        if response['payload_sha256'] != payload.sha256:
            raise ValueError('Feedback payload identity mismatch')
        public_attempt = PublicAttempt(current.case, current.strategy_document, payload,
                                       current.analysis, response['final_output'],
                                       tuple(response['packets']), origin='target_execution',
                                       error=response['error'], episode_id=request['episode_id'])
        if probe:
            from ..evidence.probe_sampling import validate_public_probe_packets
            validate_public_probe_packets(list(public_attempt.packets), context.config,
                                          episode_id=request['episode_id'])
        attempts.append(dict(ordinal=ordinal, episode_id=request['episode_id'], payload=asdict(payload),
                             final_output=public_attempt.final_output,
                             packets=list(public_attempt.packets),
                             origin=public_attempt.origin, error=public_attempt.error,
                             probe_present=_probe_present(payload, context),
                             packet_count=len(public_attempt.packets)))
        write_json(folder / 'attempts_public.json', attempts)
        active.update(stage='feedback_received', updated_at=stamp())
        write_json(folder / 'active.json', active)
        return public_attempt

    context = context_for_payload()
    try:
        payload0, generation = adapter.generate(
            public, None, None, (), 'P0', strategy_document=strategy,
            feedback_protocol=FEEDBACK_PROTOCOL, sampling=context)
    except GenerationError as exc:
        write_json(folder / 'p0_generation.json',
                   dict(status='generation_error', generation=exc.record))
        result = BranchResult('generation_error', error_type=type(exc).__name__,
                              generations=[exc.record])
        persist(result)
        _freeze(folder)
        return dict(branch=folder.name, status=result.status, target_attempts=0,
                    decisions=0, error_type=result.error_type, ablation=result.ablation)
    if context is not None:
        nonce_by_sha[payload0.sha256] = context
    write_json(folder / 'p0_generation.json',
               dict(status=generation.get('status'), payload=payload0.text,
                    payload_sha256=payload0.sha256, analysis=generation.get('analysis', ''),
                    sampling=context.public() if context is not None else None,
                    generation=generation))
    stub = PublicAttempt(public, strategy, payload0, '', '', (),
                         origin='target_execution')
    try:
        received = source(stub, payload0, 1)
    except Exception as exc:
        result = BranchResult('target_error', error_type=type(exc).__name__,
                              target_attempts=1)
        persist(result)
        _freeze(folder)
        return dict(branch=folder.name, status=result.status, target_attempts=1,
                    decisions=0, error_type=result.error_type, ablation=result.ablation)
    result = FeedbackRunner(adapter, feedback_view=job['view'], target_budget=TARGET_BUDGET,
                            source=source, persist=persist,
                            initial_attempts=1).run(received)
    _freeze(folder)
    return dict(branch=folder.name, status=result.status,
                target_attempts=result.target_attempts,
                decisions=len(result.decisions), error_type=result.error_type,
                ablation=result.ablation)


def campaign(args):
    modules = AssessmentModules(args.ablation)
    mode = args.mode
    if mode not in VIEWS:
        raise ValueError('Unknown sampling mode')
    if args.ablation != GROUPS[mode]:
        raise ValueError(f'{mode} mode requires base ablation {GROUPS[mode]}')
    if args.channel is None and mode == 'probe':
        raise ValueError('Probe mode requires --channel')
    validation = validate_sampling(args.prepared, args.selection, args.channel, mode)
    channel = validation['channel']
    target_config = dict(model='gpt-5-nano', benchmark_version='v1.2.2',
                         reasoning_effort=None, request_timeout=120, max_tool_calls=30)
    if channel is not None and mode == 'probe':
        target_config['probe_sampling'] = deepcopy(channel)
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError('Output exists; no implicit resume or resampling')
    if args.dry_run:
        print(json.dumps(dict(validation, ablation=_ablation(modules, mode),
                              target=target_config)))
        return
    configure()
    _, instances = selection(args.prepared, args.selection)
    if [label['instance_id'] for _, label in instances] != validation['instances']:
        raise ValueError('Selection changed after validation')
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'selection.json', dict(schema_version=1, tasks=[dict(
        seed_id=label['seed_id'], mode=label['mode'],
        response_role=label['response_role'], enabled=True) for _, label in instances]))
    mapping = []
    jobs = []
    for index, (public, label) in enumerate(instances):
        folder = output / 'branches' / f'b{index:03d}'
        job = dict(protocol=PROTOCOL, mode=mode, input=public, model='deepseek-flash[1m]',
                   effort='max', timeout=900, view=VIEWS[mode], ablation=args.ablation,
                   ablation_protocol=ABLATION_PROTOCOL, target_config=deepcopy(target_config),
                   budget=TARGET_BUDGET)
        write_json(folder / 'job_public.json', job)
        mapping.append(dict(branch=folder.name, **label))
        jobs.append(folder)
    write_json(output / 'selection_private.json', mapping)
    sources = list((ROOT / 'src').rglob('*.py')) + [ROOT / 'scripts/run_observable_pilot5_20261029.py']
    write_json(output / 'manifest.json', dict(
        started_at=stamp(), result_kind='LIVE_JAILBREAK_EVIDENCE_SAMPLING_PILOT',
        protocol=PROTOCOL, mode=mode, view=VIEWS[mode], method='piminer',
        probe_payload_policy=PAYLOAD_POLICY if mode == 'probe' else 'disabled',
        model='deepseek-flash[1m]', effort='max', target=target_config,
        workers=args.workers, target_budget_per_branch=TARGET_BUDGET,
        max_target_dispatches=TARGET_BUDGET * len(instances), target_runs=0,
        resumable=False, initial_payload='attacker_generated_sampling_aware_p0',
        initial_feedback='none; Attacker-generated P0 is executed before assessment',
        channel=channel, channel_sha256=(digest(args.channel) if args.channel else None),
        ablation=_ablation(modules, mode),
        validation=validation,
        input_sha256={str(path): digest(path) for path in [
            Path(args.prepared) / 'manifest.json',
            Path(args.prepared) / 'model_inputs.jsonl',
            Path(args.prepared) / 'scoring_private.jsonl']},
        code_sha256={str(path.relative_to(ROOT)): digest(path) for path in sources}))
    os.environ.update(PIMINER_CALL_LOG=str(output / 'preflight_calls.jsonl'),
                      PIMINER_STOP_FILE=str(output / 'STOP.json'), PIMINER_RUN_ID=output.name)
    write_json(output / 'status.json', dict(status='preflight', completed=0,
                                            total=len(instances), started_at=stamp()))
    from ..adapters.agentdojo import AgentDojoAdapter
    try:
        preflight = dict(target=AgentDojoAdapter(**target_config).preflight(),
                         attacker=PIMinerAdapter(ROOT, 'deepseek-flash[1m]', 'max', 900).preflight())
        write_json(output / 'preflight.json', preflight)
    except Exception as exc:
        write_json(output / 'status.json', dict(status='preflight_error',
                                                error_type=type(exc).__name__,
                                                completed=0, total=len(instances),
                                                updated_at=stamp()))
        raise
    write_json(output / 'status.json', dict(status='running', completed=0,
                                            total=len(instances), started_at=stamp()))
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_sampling_branch, str(job)): job.name for job in jobs}
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                row = dict(branch=futures[future], status='worker_error',
                           error_type=type(exc).__name__)
            results.append(row)
            write_json(output / 'progress.json', sorted(results, key=lambda row: row['branch']))
            write_json(output / 'status.json', dict(status='running', completed=len(results),
                       total=len(instances), updated_at=stamp(), results=results))
            print(json.dumps(row), flush=True)
    write_json(output / 'status.json', dict(status='predictions_frozen',
               completed=len(instances), total=len(instances), updated_at=stamp(),
               results=results))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--prepared', type=Path,
                     default=ROOT / 'data/mismatch_autonomous_20261104')
    run.add_argument('--selection', type=Path, required=True)
    run.add_argument('--channel', type=Path)
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--mode', choices=sorted(VIEWS), required=True)
    run.add_argument('--ablation', choices=sorted(set(GROUPS.values())))
    run.add_argument('--workers', type=positive_workers, default=1)
    run.add_argument('--dry-run', action='store_true')
    score = sub.add_parser('score')
    score.add_argument('--run', type=Path, required=True)
    score.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if getattr(args, 'ablation', None) is None and hasattr(args, 'mode'):
        args.ablation = GROUPS[args.mode]
    if args.command == 'score':
        from ..evaluation.probe_sampling import score_run
        result = score_run(args.run, args.output)
        print(json.dumps({key: result[key] for key in (
            'result_kind', 'mode', 'probe_candidates_executed',
            'query_induction_rate', 'legal_return_rate', 'joint_success_rate',
            'attack_success_at_budget')}))
    else:
        campaign(args)


if __name__ == '__main__':
    main()
