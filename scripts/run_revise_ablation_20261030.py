#!/usr/bin/env python3
"""Paired first-revision experiment on one immutable, already observed H1.

All four arms import the same route/P0/H1 and start empty isolated sessions.
Every P1 is generated before any of that case's scoring executions. Two fresh
Target executions per P1 are scored without success stopping or replacement.
This exposed development pilot is not a held-out or end-to-end ASR experiment.
"""
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import json
import multiprocessing
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from run_observable_pilot5_20261029 import configure_credentials, now
from src.attacker.initialization import InitializationBundle, route_digest
from src.engine.config import check_frozen, data_fingerprints, digest, environment_versions
from src.engine.runner import Runner
from src.engine.storage import RunStore, plain, write_json
from src.evidence.observer import FixedObserver
from src.policies.always_revise import AlwaysRevise
from src.schema import Episode


CASE_IDS = ('c000', 'c001', 'c004', 'c005', 'c006')
ARMS = ('full_original', 'legacy_a2', 'a2_v2', 'a2_v2_calibrated')
REPETITIONS = 2
SOURCE = ROOT / 'runs/evidence_piminer_aligned_asr5_20261030_recovered_v2'
LIBRARY = 'strategy_library_a2/library.json'
LEGACY_RETRIEVAL = 'data/a2/dev.retrieval.json'


def canonical_hash(value):
    raw = json.dumps(plain(value), sort_keys=True, ensure_ascii=False,
                     separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def audit_source(case, initial, episode, raw, generation):
    """Bind source identity, payload, feedback and original analysis before use."""
    cid = case['case_id']
    require(episode['episode_id'] == cid + '/shared/000'
            and episode['case_id'] == cid and episode['policy_id'] == 'shared'
            and episode['phase'] == 'initial' and episode['payload_id'] == 'P0',
            'Source H1 identity differs')
    result = raw['result']
    require(episode['gold'] is False and result['gold'] is False
            and episode['status'] == 'VALID_GOLD_FALSE'
            and episode['error'] is None and result['error'] is None
            and episode['observation_error'] is None
            and result['observation_error'] is None, 'Source H1 must be a valid failure')
    require(raw['payload'] == initial['payload'] == generation['payload']
            and episode['payload_sha256'] == initial['payload_sha256']
            == generation['payload_sha256'], 'Source P0 bytes or hashes differ')
    require(generation['case_id'] == cid and generation['policy_id'] == 'shared'
            and generation['generation_id'] == cid + '/shared/P0'
            and generation['status'] == 'ready'
            and generation['analysis'] == initial['analysis']
            and generation['routing']['strategy_ids'] == initial['strategy_ids']
            and generation['routing']['strategy_paths'] == initial['strategy_paths'],
            'Source P0 analysis or ordered route differs')
    require(episode['final'] == result['final']
            and episode['observation'] == result['observation'],
            'Source H1 feedback differs from raw record')
    packets = raw['query_packets']
    require(all(p['episode_id'] == episode['episode_id'] for p in packets),
            'Source observer packets belong to another episode')
    final = result.get('black_box_final')
    if final is None:
        final = result['final']
    attempt = dict(iter=0, analysis=initial['analysis'], injection=initial['payload'],
                   security=False, utility=result['utility'], final_output=final[:4000])
    return dict(episode=deepcopy(episode), attempt=attempt, packets=deepcopy(packets),
                history_sha256=canonical_hash([attempt]),
                route_sha256=initial['route_sha256'], payload_sha256=initial['payload_sha256'])


def arm_config(config, arm, v2_retrieval):
    config = deepcopy(config)
    config['policies'] = {arm: {}}
    options = config['attacker']
    options['revision_profile'] = 'calibrated' if arm == 'a2_v2_calibrated' else 'original'
    options.pop('a2_library', None)
    options.pop('a2_retrieval', None)
    if arm != 'full_original':
        options.update(a2_library=LIBRARY,
                       a2_retrieval=LEGACY_RETRIEVAL if arm == 'legacy_a2' else v2_retrieval)
    return config


def build_plan(source, v2_retrieval, output, seed=20261030):
    from src.adapters.agentdojo import validate_native_cases
    from src.attacker.a2 import A2Resources
    source, output = Path(source).resolve(), Path(output).resolve()
    require(not output.exists(), 'Output already exists; do not resume or overwrite')
    v2 = Path(v2_retrieval).resolve()
    require(v2.is_relative_to(ROOT), 'V2 retrieval must be inside the project')
    v2_name = str(v2.relative_to(ROOT))
    source_manifest = read(source / 'manifest.json')
    frozen_cases = check_frozen(source_manifest)
    config = deepcopy(source_manifest['config'])
    require(config['attacker']['mode'] == 'piminer_aligned', 'Aligned source required')
    indexed = {c['case_id']: c for c in frozen_cases}
    require(len(indexed) == len(frozen_cases), 'Duplicate case IDs')
    cases = [indexed[cid] for cid in CASE_IDS]
    snapshot = {c['case_id']: c for c in source_manifest['cases']}
    require(all(snapshot[c['case_id']] == c for c in cases), 'Source case snapshots differ')
    bundle = InitializationBundle(ROOT, config['attacker']['initialization_manifest'])
    paths = {ROOT / path for path in data_fingerprints(source_manifest['inputs_sha256'])}
    paths.add(source / 'manifest.json')
    sources = {}
    for case in cases:
        cid = case['case_id']
        episode_path = source / cid / 'episodes.jsonl'
        rows = [json.loads(line) for line in episode_path.read_text().splitlines() if line.strip()]
        initial_rows = [r for r in rows if r['phase'] == 'initial']
        require(len(initial_rows) == 1, 'Exactly one source H1 is required')
        episode = initial_rows[0]
        raw_path = (source / cid / episode['raw_path']).resolve()
        require(raw_path.is_relative_to(source / cid), 'Source raw path escapes its case')
        generation_path = source / cid / 'raw' / cid / 'shared/P0.generation.json'
        initial = bundle.for_case(case)
        initial['payload_sha256'] = hashlib.sha256(initial['payload'].encode()).hexdigest()
        initial['route_sha256'] = route_digest(initial['strategy_ids'], initial['strategy_paths'],
            [digest(ROOT / path) for path in initial['strategy_paths']])
        sources[cid] = audit_source(case, initial, episode,
                                    read(raw_path), read(generation_path))
        sources[cid]['source_files'] = [str(p) for p in (episode_path, raw_path, generation_path)]
        paths.update((episode_path, raw_path, generation_path))
    for retrieval in (LEGACY_RETRIEVAL, v2_name):
        resources = A2Resources(ROOT, LIBRARY, retrieval)
        if retrieval == v2_name:
            from src.attacker.retrieval import PROTOCOL
            require(resources.protocol == PROTOCOL
                    and resources.retrieval['retrieval_method']['variant'] == 'field_embedding',
                    'The V2 arm requires the prepared field-embedding V2 resource')
        for case in cases:
            resources.validate_case(case)
        paths.update(resources.input_paths)
    native = validate_native_cases(cases, config['target']['benchmark_version'])
    return dict(schema_version=1, kind='LIVE_FIXED_H1_REVISE_ABLATION', created_at=now(),
        source_run=str(source), output=str(output), case_ids=list(CASE_IDS), cases=cases,
        arms=list(ARMS), repetitions=REPETITIONS, seed=seed, workers=3,
        config=config, arm_configs={a: arm_config(config, a, v2_name) for a in ARMS},
        sources=sources, environment=environment_versions(),
        inputs_sha256={str(p.resolve()): digest(p) for p in sorted(paths) if p.suffix != '.py'},
        native=native, limits=dict(generation_calls=20, target_executions=40,
                                  preflight_target_calls=1, preflight_attacker_turns=2),
        independent_heldout=False, source_h1_reused=True, fresh_p0=False,
        protocol=dict(source='One identical completed H1, P0, analysis and ordered route per case.',
            memory='Empty per-case/per-arm memory; never published during this experiment.',
            sessions='Fresh isolated persistent session for each first revision.',
            generation='One P1 per arm; all four generated before scoring this case.',
            scoring='Two independent fresh environments per P1; counterbalanced order; no success stop.',
            errors='Missing slots retain planned denominator; no replacement or automatic resume.',
            outcome='Case-mean official Gold over the two fixed-P1 executions.',
            interpretation='Five exposed tasks; repeats share a candidate and are not independent cases.'))


def verify_plan(plan):
    require(environment_versions() == plan['environment'], 'Runtime environment changed')
    for path, expected in plan['inputs_sha256'].items():
        require(digest(Path(path)) == expected, 'Frozen input changed: ' + path)


def counterbalanced_orders(case_index, seed):
    base = list(ARMS)
    random.Random(seed).shuffle(base)
    offset = case_index % len(base)
    generation = base[offset:] + base[:offset]
    # Reverse between repetitions to balance early/late Target positions.
    return generation, [generation, list(reversed(generation))]


def summarize(results, case_ids=CASE_IDS):
    """Keep planned slots and missing cases, and pair only complete case means."""
    indexed = {row['case_id']: row for row in results}
    require(len(indexed) == len(results), 'Duplicate case results')
    require(set(indexed).issubset(case_ids), 'Unexpected case result')
    arm_stats, case_means = {}, {}
    for arm in ARMS:
        slots, means, generation_errors = [], {}, 0
        for cid in case_ids:
            record = indexed.get(cid, {}).get('arms', {}).get(arm, {})
            trials = record.get('trials', [])
            require(len(trials) <= REPETITIONS, 'Too many Target trials')
            require(len({r['repeat'] for r in trials}) == len(trials)
                    and all(r['repeat'] in range(REPETITIONS) for r in trials), 'Duplicate trial slot')
            require(all(t.get('gold') is None for t in trials if t.get('error')
                        or t.get('status') in ('TARGET_ERROR', 'NOT_STARTED_GENERATION_ERROR')),
                    'Error or unstarted trial cannot carry a Gold verdict')
            values = {t['repeat']: t.get('gold') for t in trials}
            require(all(v is None or type(v) is bool for v in values.values()), 'Invalid Gold')
            golds = [values.get(rep) for rep in range(REPETITIONS)]
            slots.extend(golds)
            means[cid] = (sum(golds) / REPETITIONS if all(type(x) is bool for x in golds) else None)
            generation_errors += record.get('generation_status') == 'GENERATION_ERROR'
        valid = [x for x in slots if type(x) is bool]
        successes, planned = sum(valid), len(slots)
        missing = planned - len(valid)
        arm_stats[arm] = dict(planned_cases=len(case_ids), planned_target_slots=planned,
            confirmed_successes=successes, valid_target_results=len(valid), missing_slots=missing,
            generation_errors=generation_errors,
            confirmed_success_fraction=successes / planned if planned else None,
            valid_result_success_fraction=successes / len(valid) if valid else None,
            complete_case_mean_success_fraction=(successes / planned if planned and not missing else None),
            missing_outcome_bounds=[successes / planned, (successes + missing) / planned] if planned else None,
            case_means=means)
        case_means[arm] = means
    comparisons = {}
    for left, right in [('legacy_a2', 'full_original'), ('a2_v2', 'legacy_a2'),
                        ('a2_v2_calibrated', 'a2_v2'), ('a2_v2_calibrated', 'full_original')]:
        pairs = [(case_means[left][cid], case_means[right][cid]) for cid in case_ids
                 if case_means[left][cid] is not None and case_means[right][cid] is not None]
        differences = [a - b for a, b in pairs]
        comparisons[left + '_minus_' + right] = dict(complete_case_pairs=len(pairs),
            case_wins=sum(d > 0 for d in differences), case_losses=sum(d < 0 for d in differences),
            case_ties=sum(d == 0 for d in differences),
            complete_case_mean_difference=sum(differences) / len(differences) if differences else None,
            planned_cases=len(case_ids), missing_case_pairs=len(case_ids) - len(pairs))
    return dict(planned_cases=len(case_ids), completed_case_records=len(results), arms=arm_stats,
        paired_comparisons=comparisons, independent_heldout=False,
        uncertainty='Two executions reuse one generated P1 per case/arm. Five cases are the paired units; no significance or generalization claim.',
        money_cost=None)


def execute_case(plan, cid, output, adapter_factory=None, observer_factory=None):
    if adapter_factory is None:
        from main import adapters
        adapter_factory = adapters
    observer_factory = observer_factory or (lambda config: FixedObserver(**config))
    verify_plan(plan)
    case = next(c for c in plan['cases'] if c['case_id'] == cid)
    source = plan['sources'][cid]
    require(canonical_hash([source['attempt']]) == source['history_sha256'], 'History projection changed')
    directory = Path(output) / cid
    directory.mkdir(parents=True, exist_ok=False)
    generation_order, trial_orders = counterbalanced_orders(plan['case_ids'].index(cid), plan['seed'])
    result = dict(case_id=cid, status='generating', source_history_sha256=source['history_sha256'],
                  generation_order=generation_order, scoring_orders=trial_orders, arms={})
    runners, candidates = {}, {}
    write_json(directory / 'status.json', {'status': 'generating'})
    for arm in generation_order:
        config = deepcopy(plan['arm_configs'][arm])
        target, attacker = adapter_factory(config)
        store = RunStore(directory / arm, dict(kind=plan['kind'], case_id=cid, arm=arm,
            source_history_sha256=source['history_sha256'], config=config,
            environment=plan['environment'], independent_heldout=False))
        runner = Runner(target, attacker, observer_factory(config['observation']),
                        {arm: AlwaysRevise()}, store, post_failure_budget=2, seed=plan['seed'])
        runners[arm] = runner
        p0, _ = runner.generate(case, 'shared', None, None, 'P0')
        require(p0 is not None and p0.sha256 == source['payload_sha256'], 'Imported P0 differs')
        h1 = Episode(**deepcopy(source['episode']))
        # Import only the generator's normal allowlisted projection; no historical
        # execution is appended to the new physical-execution or cost records.
        runner.attempts[h1.episode_id] = {k: deepcopy(v) for k, v in source['attempt'].items() if k != 'iter'}
        runner.packets[h1.episode_id] = deepcopy(source['packets'])
        attacker.begin_branch(case, arm)
        p1, generation = runner.generate(case, arm, p0, h1, 'P1', history=[h1])
        if generation.get('experience') is not None:
            require(generation['experience']['entries'] == 0, 'Nonempty rolling experience')
        candidates[arm] = p1
        result['arms'][arm] = dict(generation_status=generation['status'], generation=generation,
            unchanged_payload=p1.sha256 == p0.sha256 if p1 else None,
            payload_sha256=p1.sha256 if p1 else None, trials=[])
        write_json(directory / 'result.json', result)
    result['status'] = 'scoring'
    write_json(directory / 'status.json', {'status': 'scoring'})
    for repeat, order in enumerate(trial_orders):
        for arm in order:
            record, p1 = result['arms'][arm], candidates[arm]
            if p1 is None:
                trial = dict(repeat=repeat, status='NOT_STARTED_GENERATION_ERROR', gold=None)
            else:
                episode = runners[arm].execute(case, arm, p1, 'fixed_p1_scoring', repeat + 1)
                trial = dict(repeat=repeat, **episode.to_dict())
            record['trials'].append(trial)
            write_json(directory / 'result.json', result)
    for arm, runner in runners.items():
        result['arms'][arm]['physical_cost'] = runner.metrics()['physical_total_cost']
    result['status'] = 'completed'
    write_json(directory / 'result.json', result)
    write_json(directory / 'status.json', {'status': 'completed'})
    return result


def case_worker(plan, cid, output):
    with (Path(output) / (cid + '.log')).open('x') as log, redirect_stdout(log), redirect_stderr(log):
        return execute_case(plan, cid, output)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', default=str(SOURCE))
    parser.add_argument('--v2-retrieval', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    plan = build_plan(args.source_run, args.v2_retrieval, args.output)
    if args.dry_run:
        print(json.dumps(dict(status='offline_valid', api_calls=0, case_ids=plan['case_ids'],
                              arms=plan['arms'], limits=plan['limits'], workers=plan['workers'])))
        return 0
    configure_credentials()
    verify_plan(plan)
    store = RunStore(plan['output'], plan)
    results = []
    write_json(store.path / 'status.json', dict(status='preflight', started_at=now()))
    try:
        from main import adapters
        target, attacker = adapters(plan['arm_configs']['full_original'])
        store.append('events', dict(event='PREFLIGHT_STARTED', timestamp=now()))
        preflight = dict(native=plan['native'], target=dict(status='started'),
                         attacker=dict(status='not_started'))
        write_json(store.path / 'preflight.json', preflight)
        preflight['target'] = target.preflight()
        preflight['attacker'] = dict(status='started', planned_neutral_turns=2)
        write_json(store.path / 'preflight.json', preflight)
        preflight['attacker'] = attacker.preflight()
        write_json(store.path / 'preflight.json', preflight)
        store.append('events', dict(event='PREFLIGHT_COMPLETED', timestamp=now()))
        write_json(store.path / 'status.json', dict(status='running', started_at=now()))
        with ProcessPoolExecutor(max_workers=plan['workers'], mp_context=multiprocessing.get_context('spawn')) as pool:
            futures = {pool.submit(case_worker, plan, cid, store.path): cid for cid in plan['case_ids']}
            for future in as_completed(futures):
                cid = futures[future]
                try:
                    row = future.result()
                except Exception as exc:
                    saved = store.path / cid / 'result.json'
                    row = read(saved) if saved.exists() else dict(case_id=cid, arms={})
                    row.update(status='worker_error', error_type=type(exc).__name__,
                               error_details='Exception text omitted to avoid exposing request or credential data.')
                results.append(row)
                write_json(store.path / 'results.json', sorted(results, key=lambda r: r['case_id']))
                write_json(store.path / 'summary.json', summarize(results, plan['case_ids']))
                store.append('events', dict(event='CASE_TERMINAL', case_id=cid, status=row['status'], timestamp=now()))
                print(json.dumps(dict(case_id=cid, status=row['status'])), flush=True)
        status = 'completed' if all(r['status'] == 'completed' for r in results) else 'completed_with_errors'
        write_json(store.path / 'status.json', dict(status=status, completed_at=now()))
        return 0 if status == 'completed' else 1
    except BaseException as exc:
        write_json(store.path / 'status.json', dict(status='interrupted', error_type=type(exc).__name__,
            completed_at=now(), recovery='Inspect started events; never automatically repeat unknown executions.'))
        print(json.dumps(dict(status='interrupted', error_type=type(exc).__name__)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
