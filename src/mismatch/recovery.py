"""Recover frozen error branches into a new run; never overwrite the source."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from types import SimpleNamespace

from .data import ROOT, digest, write_json
from ..feedback.ablation import ABLATION_PROTOCOL, AssessmentModules
from .online import run_branch, stamp, positive_workers


def inspect_run(source, include_generation):
    status = json.loads((source / 'status.json').read_text())
    if status['status'] not in ('completed', 'completed_with_errors'):
        raise ValueError('Source must be a completed, frozen run')
    manifest = json.loads((source / 'manifest.json').read_text())
    from ..evidence.probe_sampling import PROTOCOL as SAMPLING_PROTOCOL
    if (manifest.get('protocol') == SAMPLING_PROTOCOL
            or manifest.get('target', {}).get('probe_sampling') is not None):
        raise ValueError('Jailbreak probe sampling runs are not resumable under the legacy recovery protocol')
    if manifest.get('target', {}).get('active_verification') is not None:
        from ..evidence.active_verification import validate_config
        validate_config(manifest['target']['active_verification'])
    if manifest.get('ablation', {}).get('protocol') != ABLATION_PROTOCOL:
        raise ValueError('Legacy ablation protocol cannot be resumed under v3; start a fresh run')
    plans = []
    for row in sorted(status['results'], key=lambda x: x['branch']):
        folder = source / 'branches' / row['branch']
        frozen = json.loads((folder / 'frozen.json').read_text())
        for name, expected in frozen['files'].items():
            if digest(folder / name) != expected:
                raise ValueError('Source checksum mismatch: ' + str(folder / name))
        job = json.loads((folder / 'job_public.json').read_text())
        if job.get('target_config', {}).get('active_verification') is not None:
            from ..evidence.active_verification import validate_config
            validate_config(job['target_config']['active_verification'])
        expected = AssessmentModules(job.get('ablation')).metadata()
        if (job.get('ablation_protocol') != ABLATION_PROTOCOL
                or job['view'] != expected['feedback_view']
                or manifest['ablation'] != expected
                or row.get('ablation') != expected):
            raise ValueError('Recovery ablation configuration mismatch')
        selected = row.get('error_type') == 'RateLimitError' or (
            include_generation and row.get('error_type') == 'GenerationError')
        if not selected:
            continue
        calls = [json.loads(p.read_text()) for p in sorted((folder / 'calls').glob('*.json'))]
        # Resume at controller boundaries, never replay Planner internals.
        branch = json.loads((folder / 'branch.json').read_text())
        calls = [c for c in calls if c['stage'] == 'assessment' and c['status'] == 'completed']
        if len(calls) != len(branch['decisions']):
            raise ValueError('Assessment cache and decisions differ')
        revisions = {}
        generations = iter(branch['generations'])
        for index, decision in enumerate(branch['decisions']):
            if decision['action'] != 'Revise':
                continue
            generation = next(generations, None)
            if generation is None:
                break
            if generation['status'] == 'ready':
                revisions[str(index + 1)] = generation
            else:
                break
        executions = {}
        for path in sorted((folder / 'executions').glob('*')):
            response = json.loads((path / 'feedback_public.json').read_text())
            if response['error']:
                break
            executions[str(int(path.name))] = str(path)
        plans.append(dict(branch=row['branch'], error_type=row['error_type'],
                          calls=calls, revisions=revisions, executions=executions,
                          prior_target_dispatches=row['target_attempts']))
    if not plans:
        raise ValueError('No eligible error branches')
    return status, manifest, plans


def _recover(args):
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists() or source in output.parents:
        raise ValueError('Use a new output directory outside the source run')
    status, manifest, plans = inspect_run(source, args.include_generation_errors)
    summary = dict(source=str(source), output=str(output), workers=args.workers,
        branches=[{k:v for k,v in p.items() if k not in ('calls','executions','revisions')} |
                  dict(replayed_assessments=len(p['calls']), reused_revisions=len(p['revisions']), reused_executions=len(p['executions']))
                  for p in plans],
        policy='reuse completed assessments and candidate artifacts; never replay planner internals',
        api_calls=0 if args.dry_run else None)
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary
    output.mkdir(parents=True)
    write_json(output / 'recovery.json', summary)
    for name in ('selection.json', 'selection_private.json'):
        shutil.copy2(source / name, output / name)
    by_branch = {p['branch']:p for p in plans}
    jobs, results = [], []
    for row in status['results']:
        name = row['branch']
        old, new = source/'branches'/name, output/'branches'/name
        if name not in by_branch:
            shutil.copytree(old, new)
            results.append(row)
            continue
        plan = by_branch[name]
        job = json.loads((old/'job_public.json').read_text())
        snapshots = {}
        for ordinal, path in plan['executions'].items():
            dest = new/'recovery_prefix'/ordinal
            shutil.copytree(path, dest)
            snapshots[ordinal] = str(dest)
        job['recovery'] = dict(protocol='controller_boundaries_v2', calls=plan['calls'], revisions=plan['revisions'], executions=snapshots)
        write_json(new/'job_public.json', job)
        jobs.append(new)
    manifest.update(started_at=stamp(), workers=args.workers, recovery=summary,
        source_manifest_sha256=digest(source/'manifest.json'),
        recovery_code_sha256={str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'src').rglob('*.py')},
        rate_limit_retry=dict(retries=os.environ.get('PIMINER_RATE_LIMIT_RETRIES','6'),
                             base=os.environ.get('PIMINER_RATE_LIMIT_BASE','2'),
                             cap=os.environ.get('PIMINER_RATE_LIMIT_CAP','60')))
    write_json(output/'manifest.json', manifest)
    def persist(state):
        write_json(output/'status.json',dict(status=state, completed=len(results),
                   total=status['total'], updated_at=stamp(), results=results))
    persist('running')
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_branch,str(p)):p.name for p in jobs}
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                row = dict(branch=futures[future], status='worker_error', error_type=type(exc).__name__)
            results.append(row)
            persist('running')
            print(json.dumps(row), flush=True)
    persist('predictions_frozen')
    subprocess.run([sys.executable,'-m','src.evaluation.mismatch_online','--run',str(output)],cwd=ROOT,check=True)
    persist('completed_with_errors' if any(r['status'].endswith('_error') for r in results) else 'completed')
    with (output/'RESULTS.md').open('a') as report:
        report.write('\n恢复运行：保留源运行已完成前缀；失败的Target槽位可重新执行，GenerationError可重新采样生成。'
                     '源运行额外失败调用保留在原目录，因此本报告的逻辑ASR@5不代表总物理调用成本。'
                     '此结果应与原始运行并列报告。\n')


def publish(local, destination):
    """Copy to a sibling staging directory, verify every file, then publish."""
    local, destination = Path(local), Path(destination)
    if destination.exists():
        raise FileExistsError('Final output already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + '.copying-' + uuid.uuid4().hex[:8])
    shutil.copytree(local, staging)
    for path in local.rglob('*'):
        if path.is_file() and digest(path) != digest(staging / path.relative_to(local)):
            raise ValueError('Copy checksum mismatch: ' + str(path))
    if destination.exists():
        raise FileExistsError('Final output appeared during copying')
    staging.rename(destination)
    return destination


def recover(args):
    local_root = getattr(args, 'local_root', None)
    if local_root is None:
        return _recover(args)
    destination = args.output.resolve()
    if destination.exists():
        raise FileExistsError('Final output exists; use a new directory')
    if args.source.resolve() in destination.parents:
        raise ValueError('Final output must be outside source run')
    local = Path(local_root).expanduser().resolve() / destination.name
    if local == destination:
        raise ValueError('Local and final directories must differ')
    options = SimpleNamespace(**vars(args))
    options.output = local
    if args.dry_run:
        result = _recover(options)
        print(json.dumps(dict(local_output=str(local), final_output=str(destination),
                              publication='verified copy after completion', api_calls=0)))
        return result
    print(json.dumps(dict(local_output=str(local), final_output=str(destination))), flush=True)
    # Interruption or copy failure leaves local records intact.
    _recover(options)
    write_json(local/'storage.json', dict(local_output=str(local),
        final_output=str(destination), policy='local execution; verified copy; retain local source'))
    publish(local, destination)
    print(json.dumps(dict(published=str(destination), retained_local=str(local))), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--local-root', type=Path, default=Path.home()/'temp_run',
                        help='Local working root; verified completed results are copied to --output')
    parser.add_argument('--workers',type=positive_workers,default=2)
    parser.add_argument('--include-generation-errors',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    recover(parser.parse_args())


if __name__ == '__main__':
    main()
