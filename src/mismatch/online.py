"""Selected V1 branches: public controller, isolated execution, post-hoc scoring."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
from zoneinfo import ZoneInfo

from .data import ROOT, digest, jsonl, load_attempt, write_json, write_jsonl
from ..feedback.ablation import ABLATION_PROTOCOL, AssessmentModules, add_arguments

PYTHON = sys.executable


def stamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def configure():
    # Adopt the same credentials and endpoints as the prior native experiments.
    sys.path.insert(0, str(ROOT / 'scripts'))
    from run_observable_pilot5_20261029 import configure_credentials
    configure_credentials()


def live_packets(result, suite):
    """Only returned permitted write calls, with no private state or score fields."""
    from .replay_observer import replay_packets
    from agentdojo.types import get_text_content_as_str
    if (result.get('raw') or {}).get('probe_sampling') is not None:
        raise ValueError('Probe sampling diagnostics cannot be projected as legacy write receipts')
    steps = []
    for message in result['raw'].get('messages', []):
        if message.get('role') != 'tool':
            continue
        call = message['tool_call']
        steps.append(dict(call={k: call[k] for k in ('function', 'args')},
                          result=get_text_content_as_str(message['content']),
                          error=message.get('error')))
    # Existing Mismatch write-receipt experiment only. Framework-assisted read
    # diagnostics (including their final report) never cross this boundary.
    return replay_packets(dict(steps=steps), suite)


def target_job(request_path):
    from .tasks import MismatchTargetAdapter
    from ..schema import Payload
    configure()
    request = json.loads(Path(request_path).read_text())
    folder = Path(request_path).parent
    os.environ['PIMINER_CALL_LOG'] = str(folder / 'target_calls.jsonl')
    target_config = request['target_config']
    if (target_config.get('probe_sampling') is not None
            and target_config.get('active_verification') is not None):
        raise ValueError('Framework verification and jailbreak probe sampling are separate protocols')
    adapter = MismatchTargetAdapter(**target_config)
    result = adapter.execute(request['case'], Payload(**request['payload']), request['episode_id'])
    private = asdict(result)
    write_json(folder / 'execution_private.json', private)
    projection = request.get('feedback_projection', 'legacy_write_receipts')
    probe = target_config.get('probe_sampling')
    if probe is not None and projection != 'probe_receiver':
        raise ValueError('Probe sampling requires the probe_receiver feedback projection')
    if projection == 'probe_receiver':
        if probe is None:
            raise ValueError('Probe_receiver projection requires a probe sampling config')
        from ..evidence.probe_sampling import (
            public_packets as probe_public_packets,
            validate_public_probe_packets,
        )
        packets = probe_public_packets((private.get('raw') or {}).get('probe_sampling'))
        validate_public_probe_packets(packets, probe, episode_id=request['episode_id'])
    elif projection == 'none':
        packets = []
    elif projection == 'legacy_write_receipts':
        # Existing Mismatch write-receipt experiment only. Framework-assisted read
        # diagnostics (including their final report) never cross this boundary.
        packets = live_packets(private, request['case']['suite_name'])
    else:
        raise ValueError('Unknown feedback projection')
    final_projection = request.get('final_projection', 'black_box')
    if final_projection == 'text_only':
        from .final_projection import extract_final_text_only
        final_output = extract_final_text_only((private.get('raw') or {}).get('messages', []))
    elif final_projection == 'black_box':
        final_output = (result.black_box_final if result.black_box_final is not None
                        else result.final)
    else:
        raise ValueError('Unknown final projection')
    public = dict(final_output=final_output,
                  packets=packets, error=result.error,
                  payload_sha256=Payload(**request['payload']).sha256)
    write_json(folder / 'feedback_public.json', public)


def run_branch(branch_path):
    """This process reads public inputs; the scorer is a separate later process."""
    from ..attacker.piminer import PIMinerAdapter
    from ..engine.feedback_runner import FeedbackRunner
    folder = Path(branch_path)
    job = json.loads((folder / 'job_public.json').read_text())
    if job.get('target_config', {}).get('probe_sampling') is not None:
        raise ValueError('Jailbreak probe sampling requires the dedicated sampling campaign')
    if job.get('target_config', {}).get('active_verification') is not None:
        from ..evidence.active_verification import validate_config
        validate_config(job['target_config']['active_verification'])
    if job.get('ablation_protocol') != ABLATION_PROTOCOL:
        raise ValueError('Legacy or missing ablation protocol; start a fresh v3 run')
    modules = AssessmentModules(job.get('ablation'), job.get('judgment_memory'))
    if job['view'] != modules.config.feedback_view:
        raise ValueError('Job feedback view conflicts with ablation group')
    configure()
    os.environ.update(PIMINER_CALL_LOG=str(folder / 'provider_calls.jsonl'),
                      PIMINER_STOP_FILE=str(folder / 'STOP.json'), PIMINER_RUN_ID=folder.name)
    initial = load_attempt(job['input'])
    adapter = PIMinerAdapter(ROOT, job['model'], job['effort'], job['timeout'],
                            ablation=modules.config)
    transport = adapter.decide
    calls = []
    replay = job.get('recovery', {})
    if replay and replay.get('protocol') != 'controller_boundaries_v2':
        raise ValueError('Legacy recovery cache: prepare a new recovery from its frozen source')
    cached_calls = list(replay.get('calls', []))
    cached_revisions = replay.get('revisions', {})
    original_revise = adapter.revise

    def recover_revision(attempt, history, decision, *, feedback_view):
        from copy import deepcopy
        from ..schema import Payload
        cached = cached_revisions.get(str(len(history)))
        if cached is None:
            return original_revise(attempt, history, decision, feedback_view=feedback_view)
        if cached['parent_payload_sha256'] != attempt.payload.sha256:
            raise ValueError('Cached revision parent differs from completed history')
        payload = Payload(f'P{len(history)}', cached['payload'])
        if payload.sha256 != cached['payload_sha256'] or cached['status'] != 'ready':
            raise ValueError('Invalid completed revision artifact')
        record = deepcopy(cached)
        record['replayed_artifact'] = True
        return payload, record

    adapter.revise = recover_revision
    cached_executions = replay.get('executions', {})
    attempts = [dict(ordinal=0, payload=asdict(initial.payload), final_output=initial.final_output,
                     packets=list(initial.packets), origin=initial.origin)]
    active = dict(stage='starting', updated_at=stamp())

    def tracked(prompt, stage):
        index = len(calls)
        item = dict(stage=stage, status='running', started_at=stamp(), prompt=prompt)
        calls.append(item)
        active.update(stage=stage, updated_at=stamp())
        write_json(folder / 'active.json', active)
        write_json(folder / 'calls' / f'{index:03d}.json', item)
        before = len(transport.calls)
        try:
            if stage == 'assessment' and cached_calls:
                cached = cached_calls[0]
                if cached['prompt'] != prompt or cached['stage'] != stage:
                    raise ValueError('Recovery prompt differs from frozen prefix')
                output = cached['output']
                item['replayed'] = True
                cached_calls.pop(0)
            else:
                output = transport(prompt, stage)
            item.update(status='completed', output=output)
            return output
        except Exception as exc:
            item.update(status='error', error_type=type(exc).__name__)
            raise
        finally:
            item['finished_at'] = stamp()
            if len(transport.calls) > before:
                item['provider'] = transport.calls[-1]
            write_json(folder / 'calls' / f'{index:03d}.json', item)

    def persist(result):
        write_json(folder / 'branch.json', asdict(result))
        active.update(status=result.status, target_attempts=result.target_attempts, updated_at=stamp())
        write_json(folder / 'active.json', active)

    def source(current, payload, ordinal):
        execution = folder / 'executions' / f'{ordinal:02d}'
        cached = cached_executions.get(str(ordinal))
        if cached:
            old = Path(cached)
            request = json.loads((old / 'request_public.json').read_text())
            if (request['case'] != current.case or request['payload'] != asdict(payload)
                    or request.get('target_config', {}) != job['target_config']):
                raise ValueError('Recovery execution differs from frozen prefix')
            shutil.copytree(old, execution)
            response = json.loads((execution / 'feedback_public.json').read_text())
            if response['error'] or response['payload_sha256'] != payload.sha256:
                raise ValueError('Invalid recovery execution')
            public = replace(current, payload=payload, final_output=response['final_output'],
                             packets=tuple(response['packets']), error=None, origin='target_execution')
            attempts.append(dict(ordinal=ordinal, payload=asdict(payload), final_output=public.final_output,
                                 packets=list(public.packets), origin=public.origin, error=None, replayed=True))
            write_json(folder / 'attempts_public.json', attempts)
            return public
        execution.mkdir(parents=True, exist_ok=False)
        request = dict(case=current.case, payload=asdict(payload),
                       target_config=job['target_config'], episode_id=f'{folder.name}/live/{ordinal}')
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
        public = replace(current, payload=payload, final_output=response['final_output'],
                         packets=tuple(response['packets']), error=response['error'], origin='target_execution')
        attempts.append(dict(ordinal=ordinal, payload=asdict(payload), final_output=public.final_output,
                             packets=list(public.packets), origin=public.origin, error=public.error))
        write_json(folder / 'attempts_public.json', attempts)
        return public

    adapter.decide = tracked
    write_json(folder / 'attempts_public.json', attempts)
    result = FeedbackRunner(adapter, feedback_view=job['view'], target_budget=5,
                            source=source, persist=persist).run(initial)
    write_json(folder / 'frozen.json', dict(status='frozen', finished_at=stamp(),
        files={str(p.relative_to(folder)): digest(p) for p in sorted(folder.rglob('*.json'))
               if p.name not in ('frozen.json', 'active.json')}))
    return dict(branch=folder.name, status=result.status, target_attempts=result.target_attempts,
                decisions=len(result.decisions), error_type=result.error_type,
                ablation=result.ablation)


def selection(prepared, selection_path=None):
    prepared = Path(prepared)
    manifest = json.loads((prepared / 'manifest.json').read_text())
    for name, checksum in manifest['files'].items():
        if digest(prepared / name) != checksum:
            raise ValueError('Prepared inputs changed: ' + name)
    for path, checksum in manifest['source_sha256'].items():
        if digest(path) != checksum:
            raise ValueError('Source asset changed: ' + path)
    publics = {r['uid']: r for r in jsonl(prepared / 'model_inputs.jsonl')}
    labels = {r['instance_id']: r for r in jsonl(prepared / 'scoring_private.jsonl')}
    instances = []
    if selection_path is not None:
        from .selection import load_selection
        _, identities = load_selection(selection_path)
        for identity in identities:
            if identity not in labels:
                raise ValueError('Selected task is not in prepared inputs: ' + identity)
            label = labels[identity]
            instances.append((publics[label['uid']], label))
        return manifest, instances
    for mode in ('M1', 'M2', 'M3'):
        for sid in ('S01', 'S02', 'S03'):
            role = ('full' if sid == 'S02' else 'none') if mode == 'M1' else 'mismatched_response'
            label = labels[f'{sid}_{mode}:{role}']
            instances.append((publics[label['uid']], label))
    return manifest, instances


def validate(prepared, selection_path=None):
    from .tasks import validate_cases
    manifest, instances = selection(prepared, selection_path)
    cases = [dict(row['case'], case_id=label['seed_id']) for row, label in instances]
    native = validate_cases(cases)
    spec = json.loads((ROOT / 'configs/p0_stability_env_20261030.json').read_text())
    versions = {name: importlib.metadata.version(name) for name in spec['packages']}
    if versions != spec['packages']:
        raise ValueError('Existing environment differs from pinned API stack')
    return dict(status='offline_valid', instances=[s['instance_id'] for _, s in instances],
                native=native, max_target_dispatches=5*len(instances), target_budget_per_branch=5,
                api_calls=0, prepared_manifest_sha256=digest(Path(prepared) / 'manifest.json'),
                environment=dict(versions=versions, spec_sha256=hashlib.sha256(
                    json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()))


def campaign(args):
    modules = AssessmentModules(getattr(args, 'ablation', None), getattr(args, 'judgment_memory', None))
    selection_path = getattr(args, 'selection', None)
    validation = validate(args.prepared, selection_path)
    validation['ablation'] = modules.metadata()
    verification_path = getattr(args, 'active_verification', None)
    verification = None
    if verification_path:
        from ..evidence.active_verification import validate_config
        verification = validate_config(json.loads(Path(verification_path).read_text()))
        _, selected = selection(args.prepared, selection_path)
        if any(public['case']['suite_name'] != 'workspace' for public, _ in selected):
            raise ValueError('Active verification v1 requires a workspace-only selection')
        validation['active_verification'] = verification
        validation['verification_boundary'] = dict(requester='framework', instruction_channel='user',
                                                   visibility='evaluator_only', jailbreak_sampling=False)
        validation['verification_initial_feedback'] = 'unverified_static_no_live_environment'
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError('Output exists; no implicit resume or resampling')
    if args.dry_run:
        print(json.dumps(validation))
        return
    configure()
    _, instances = selection(args.prepared, selection_path)
    if [label['instance_id'] for _, label in instances] != validation['instances']:
        raise ValueError('Selection changed after validation')
    total = len(instances)
    target_config = dict(model='gpt-5-nano', benchmark_version='v1.2.2', reasoning_effort=None,
                         request_timeout=120, max_tool_calls=30)
    if verification:
        target_config['active_verification'] = verification
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'selection.json', dict(schema_version=1, tasks=[dict(
        seed_id=label['seed_id'], mode=label['mode'], response_role=label['response_role'],
        enabled=True) for _, label in instances]))
    mapping = []
    jobs = []
    for index, (public, label) in enumerate(instances):
        folder = output / 'branches' / f'b{index:03d}'
        job = dict(input=public, model='deepseek-flash[1m]', effort='max', timeout=900,
                   view=modules.config.feedback_view, target_config=target_config,
                   ablation=modules.config.group, ablation_protocol=ABLATION_PROTOCOL)
        write_json(folder / 'job_public.json', job)
        mapping.append(dict(branch=folder.name, **label))
        jobs.append(folder)
    write_json(output / 'selection_private.json', mapping)
    sources = list((ROOT / 'src').rglob('*.py')) + [ROOT / 'scripts/run_observable_pilot5_20261029.py']
    write_json(output / 'manifest.json', dict(started_at=stamp(), result_kind='LIVE_CONTROLLED_FEEDBACK_PILOT',
        method='v1', protocol='autonomous_mismatch_v1', feedback_view=modules.config.feedback_view,
        model='deepseek-flash[1m]', effort='max', target=target_config, workers=args.workers,
        target_budget_per_branch=5, max_target_dispatches=5*total, initial_target_dispatches=0,
        initial_feedback='controlled static; not caused by supplied P0',
        accurate_controls=any(label['response_role'].startswith('accurate') for _,label in instances),
        cross_instance_experience=modules.config.historical_experience,
        ablation=modules.metadata(), validation=validation,
        input_sha256={str(p): digest(p) for p in [Path(args.prepared) / 'manifest.json',
            Path(args.prepared) / 'model_inputs.jsonl', Path(args.prepared) / 'scoring_private.jsonl']},
        code_sha256={str(p.relative_to(ROOT)): digest(p) for p in sources}))
    os.environ.update(PIMINER_CALL_LOG=str(output / 'preflight_calls.jsonl'),
                      PIMINER_STOP_FILE=str(output / 'STOP.json'), PIMINER_RUN_ID=output.name)
    write_json(output / 'status.json', dict(status='preflight', completed=0, total=total, started_at=stamp()))
    from ..adapters.agentdojo import AgentDojoAdapter
    from ..attacker.piminer import PIMinerAdapter
    try:
        preflight = dict(target=AgentDojoAdapter(**target_config).preflight(),
                         attacker=PIMinerAdapter(ROOT, 'deepseek-flash[1m]', 'max', 900).preflight())
        write_json(output / 'preflight.json', preflight)
    except Exception as exc:
        write_json(output / 'status.json', dict(status='preflight_error', error_type=type(exc).__name__,
                                               completed=0, total=total, updated_at=stamp()))
        raise
    write_json(output / 'status.json', dict(status='running', completed=0, total=total, started_at=stamp()))
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_branch, str(job)): job.name for job in jobs}
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                row = dict(branch=futures[future], status='worker_error', error_type=type(exc).__name__)
            results.append(row)
            write_json(output / 'progress.json', sorted(results, key=lambda r: r['branch']))
            write_json(output / 'status.json', dict(status='running', completed=len(results), total=total,
                       updated_at=stamp(), results=results))
            print(json.dumps(row), flush=True)
    write_json(output / 'status.json', dict(status='predictions_frozen', completed=total, total=total,
               updated_at=stamp(), results=results))
    subprocess.run([PYTHON, '-m', 'src.evaluation.mismatch_online', '--run', str(output)],
                   cwd=ROOT, check=True)
    write_json(output / 'status.json', dict(status='completed_with_errors' if any(
        r['status'].endswith('_error') for r in results) else 'completed', completed=total,
        total=total, updated_at=stamp(), results=results))


def positive_workers(value):
    workers = int(value)
    if workers < 1:
        raise argparse.ArgumentTypeError('workers must be positive')
    return workers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--prepared', type=Path, default=ROOT / 'data/mismatch_autonomous_20261104')
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--workers', type=positive_workers, default=3)
    run.add_argument('--selection', type=Path, help='JSON task selection; omitted uses original nine branches')
    run.add_argument('--dry-run', action='store_true')
    run.add_argument('--framework-verification', '--active-verification', dest='active_verification', type=Path,
                     help='Framework-assisted high-trust verification JSON; evaluator-only, not jailbreak sampling')
    add_arguments(run)
    target = sub.add_parser('target-job')
    target.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'target-job':
        target_job(args.request)
    else:
        campaign(args)


if __name__ == '__main__':
    main()
