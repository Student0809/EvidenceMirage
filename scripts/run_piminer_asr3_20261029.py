"""Matched original-PIMiner revision baseline: reused H1 + at most 2 new executions.

Only initial routing, P0 and H1 are imported. The unchanged common Runner owns
all subsequent generation, policy decisions, execution, budget and error logic.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from run_observable_pilot5_20261029 import configure_credentials, now
from src.engine.config import without_source_hashes, check_frozen, digest, load_jsonl
from src.engine.runner import Runner
from src.engine.storage import RunStore, write_json
from src.evaluation.metrics import costs
from src.schema import Episode, Payload
from src.vendor import router

CASE_IDS = ['c000', 'c001', 'c004', 'c005', 'c006']


def load_initial(source, case):
    """Read only this case's initial artifacts; do not import any later history."""
    directory = Path(source) / case['case_id']
    episode_rows = load_jsonl(directory / 'episodes.jsonl')
    rows = [e for e in episode_rows if e['policy_id'] == 'shared' and e['phase'] == 'initial']
    if len(rows) != 1:
        raise ValueError('Exactly one shared initial episode required')
    episode = rows[0]
    if episode['gold'] is not False or episode['error'] is not None:
        raise ValueError('This matched pilot requires a valid failed initial execution')
    raw_path = directory / episode['raw_path']
    generation_path = directory / 'raw' / case['case_id'] / 'shared/P0.generation.json'
    raw = json.loads(raw_path.read_text())
    generation = json.loads(generation_path.read_text())
    p0 = Payload('P0', raw['payload'])
    if (generation['status'] != 'ready' or generation['payload'] != p0.text
            or generation['payload_sha256'] != p0.sha256 or episode['payload_sha256'] != p0.sha256
            or raw['result']['gold'] is not False or raw['result']['error'] is not None):
        raise ValueError('Initial payload/outcome mismatch')
    route = deepcopy(generation['routing'])
    for rel in route['strategy_paths']:
        path = (ROOT / rel).resolve()
        if not path.is_relative_to(ROOT) or digest(path) != route['strategy_sha256'][rel]:
            raise ValueError('Initial strategy bytes changed')
    route['document'] = router._load_sample_strategy(ROOT,
        dict(strategy_paths=[str(ROOT / p) for p in route['strategy_paths']]), None)
    if not route['document']:
        raise ValueError('Initial strategy document missing')
    result = raw['result']
    attempt = dict(analysis=generation['analysis'], injection=p0.text,
        security=result['gold'], utility=result['utility'],
        final_output=(result['black_box_final'] if result['black_box_final'] is not None else result['final'])[:4000])
    provenance = dict(source_case=str(directory.resolve()), episode_id=episode['episode_id'],
        payload_sha256=p0.sha256, source_files_sha256={
            str(raw_path.relative_to(Path(source))): digest(raw_path),
            str(generation_path.relative_to(Path(source))): digest(generation_path),
            str((directory / 'episodes.jsonl').relative_to(Path(source))): digest(directory / 'episodes.jsonl')})
    return dict(episode=episode, raw=raw, generation=generation, p0=p0,
                route=route, attempt=attempt, provenance=provenance,
                raw_path=raw_path, generation_path=generation_path)


class ReusedInitialRunner(Runner):
    """Intercept only the shared initial calls; retain Runner._case unchanged."""
    def __init__(self, *args, initial, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial = initial
        self.initial_generation_used = self.initial_execution_used = False
        cid = initial['episode']['case_id']
        self.attacker.routes[cid] = deepcopy(initial['route'])

    def generate(self, case, policy_id, payload, last, revision_id, history=()):
        if policy_id != 'shared':
            return super().generate(case, policy_id, payload, last, revision_id, history)
        if (self.initial_generation_used or payload is not None or last is not None
                or revision_id != 'P0' or history):
            raise ValueError('Invalid initial generation import')
        self.initial_generation_used = True
        record = deepcopy(self.initial['generation'])
        relative = 'raw/' + record['generation_id'] + '.generation.json'
        destination = self.store.path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.initial['generation_path'], destination)
        self.payload_analyses[case['case_id'], 'shared', 'P0'] = record['analysis']
        self.store.append('events', dict(event='INITIAL_GENERATION_REUSED',
            generation_id=record['generation_id'], raw_path=relative,
            source_sha256=digest(self.initial['generation_path'])))
        return self.initial['p0'], record

    def execute(self, case, policy_id, payload, phase, ordinal):
        if policy_id != 'shared':
            return super().execute(case, policy_id, payload, phase, ordinal)
        if (self.initial_execution_used or phase != 'initial' or ordinal != 0
                or payload.sha256 != self.initial['p0'].sha256):
            raise ValueError('Invalid initial execution import')
        self.initial_execution_used = True
        episode = Episode(**deepcopy(self.initial['episode']))
        destination = self.store.path / episode.raw_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.initial['raw_path'], destination)
        self.episodes.append(episode)
        self.packets[episode.episode_id] = deepcopy(self.initial['raw']['query_packets'])
        self.attempts[episode.episode_id] = deepcopy(self.initial['attempt'])
        self.store.append('episodes', episode)
        self.store.append('events', dict(event='INITIAL_EXECUTION_REUSED',
            episode_id=episode.episode_id, raw_path=episode.raw_path,
            source_sha256=digest(self.initial['raw_path']), counted_in_asr3=True,
            new_physical_target_execution=False))
        return episode

    def metrics(self):
        metrics = super().metrics()
        initial = [e for e in self.episodes if e.phase == 'initial']
        new = [e for e in self.episodes if e.phase != 'initial']
        shared_generations = [self.initial['generation']] if self.initial_generation_used else []
        metrics.update(asr_k=3,
            budget_definition='ASR@3: reused initial execution plus at most two new started Target executions.',
            reused_initial_executions=len(initial), physical_initial_executions=0,
            physical_total_cost=costs(new, self.generations),
            attributed_total_cost=costs(self.episodes, self.generations + shared_generations),
            reused_initial_cost=costs(initial, shared_generations))
        for group in metrics['policies'].values():
            group['cost_with_attributed_shared_p0'] = metrics['attributed_total_cost']
        return metrics


def validate_protocol(frozen, source):
    manifest = without_source_hashes(json.loads(Path(frozen).read_text()))
    cases = check_frozen(manifest)
    source_manifest = json.loads((Path(source) / 'manifest.json').read_text())
    source_cases = check_frozen(source_manifest)
    config = manifest['config']
    if (cases != source_cases or cases != source_manifest['cases']
            or [c['case_id'] for c in cases] != CASE_IDS
            or config['post_failure_target_budget'] != 2
            or config['policies'] != {'always_revise': {}}
            or config['backend'] != 'agentdojo' or config['attacker'].get('mode', 'original') != 'original'
            or not config.get('require_observable_trigger')):
        raise ValueError('ASR@3 matched baseline protocol mismatch')
    for key in ('target', 'attacker', 'observation', 'seed', 'dataset', 'goal_specs'):
        if config[key] != source_manifest['config'][key]:
            raise ValueError('Source configuration differs: ' + key)
    # Save and compare exact source cases, not a later independently loaded variant.
    initials = {c['case_id']: load_initial(source, c) for c in source_manifest['cases']}
    return manifest, source_manifest['cases'], initials


def run_case(frozen, source, case_id, output):
    manifest, cases, initials = validate_protocol(frozen, source)
    case = next(c for c in cases if c['case_id'] == case_id)
    config = manifest['config']
    initial = initials[case_id]
    with (Path(output).parent / (case_id + '.log')).open('x') as log, redirect_stdout(log), redirect_stderr(log):
        from main import adapters
        from src.evidence.observer import FixedObserver
        from src.policies.registry import build_policies
        target, attacker = adapters(config)
        child_manifest = dict(manifest, cases=[case], case_ids=[case_id], created_at=now(),
            parent_run=str(Path(output).parent), source_run=str(Path(source).resolve()),
            reused_initial=True, initial_provenance=initial['provenance'], asr_k=3,
            independence='previously_exposed_development_pilot', learned_controller_enabled=False)
        store = RunStore(output, child_manifest)
        runner = ReusedInitialRunner(target, attacker, FixedObserver(**config['observation']),
            build_policies(config['policies']), store, initial=initial,
            post_failure_budget=2, seed=config['seed'])
        metrics = runner.run([case])
        return dict(case_id=case_id, status='completed', outcomes=runner.outcomes,
            new_target_executions=metrics['physical_total_cost']['target_executions'],
            reused_initial_executions=metrics['reused_initial_executions'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', required=True)
    parser.add_argument('--source-run', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', type=int, default=5)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.workers <= 5:
        raise ValueError('Workers must be between 1 and 5')
    frozen, source, output = (Path(p).resolve() for p in (args.frozen, args.source_run, args.output))
    manifest, cases, initials = validate_protocol(frozen, source)
    config = manifest['config']
    from src.adapters.agentdojo import validate_native_cases
    native = validate_native_cases(cases, config['target']['benchmark_version'])
    if output.exists():
        raise FileExistsError('Output exists; never silently resume or resample')
    if args.dry_run:
        print(json.dumps(dict(status='offline_valid', cases=CASE_IDS, asr_k=3,
            native=native, max_new_target_executions=10, reused_initial_executions=5,
            max_new_revision_calls=10, new_routing_calls=0, preflight_calls=3, api_calls=0)))
        return
    configure_credentials()
    launch = dict(manifest, cases=cases, status='preflight', started_at=now(), pid=os.getpid(),
        workers=args.workers, source_run=str(source), reused_initial=True, asr_k=3,
        initial_provenance={cid: item['provenance'] for cid, item in initials.items()},
        source_manifest_sha256=digest(source / 'manifest.json'),
        independent_heldout=False, learned_controller_enabled=False,
        target_endpoint=os.environ['OPENAI_BASE_URL'], attacker_endpoint=os.environ['ANTHROPIC_BASE_URL'],
        error_semantics='Same harness as evidence arm: generation/target errors stop with missing outcome; never impute failure.')
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'manifest.json', launch)
    write_json(output / 'status.json', dict(status='preflight', started_at=now()))
    try:
        from main import adapters
        target, attacker = adapters(config)
        write_json(output / 'preflight.json', dict(native=native, target=target.preflight(), attacker=attacker.preflight()))
        del target, attacker
        results = []
        write_json(output / 'status.json', dict(status='running', started_at=launch['started_at']))
        print('Preflight passed. Original PIMiner baseline: five reused initial executions, at most ten new executions.', flush=True)
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context('spawn')) as pool:
            futures = {pool.submit(run_case, str(frozen), str(source), c['case_id'], str(output / c['case_id'])): c['case_id'] for c in cases}
            for future in as_completed(futures):
                cid = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = dict(case_id=cid, status='worker_error', error_type=type(exc).__name__)
                results.append(result)
                write_json(output / 'worker_results.json', sorted(results, key=lambda r: r['case_id']))
                print(json.dumps(result, ensure_ascii=False), flush=True)
        write_json(output / 'status.json', dict(status='completed' if all(r['status']=='completed' for r in results) else 'completed_with_errors',
            completed_at=now(), selected_cases=len(cases), completed_workers=sum(r['status']=='completed' for r in results)))
    except BaseException as exc:
        write_json(output / 'status.json', dict(status='interrupted', error_type=type(exc).__name__,
            completed_at=now(), recovery='Inspect durable logs; do not repeat uncertain executions.'))
        raise


if __name__ == '__main__':
    main()
