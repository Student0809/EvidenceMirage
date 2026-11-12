"""Paired fixed-candidate data collection through the existing execution runner.

Gold labels stay in evaluation records. Only a source history's allowlisted
context reaches the learned controller. No API connectivity checks occur here.
"""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import random
import re

from ..adapters.agentdojo import validate_native_cases
from ..adapters.fixture import FixtureTarget
from ..engine.budget import Budget
from ..engine.config import ROOT, digest, attacker_input_paths
from ..engine.history import History
from ..engine.runner import Runner
from ..engine.storage import RunStore, write_json
from ..evidence.ledger import validate_goal_spec
from ..evidence.observer import FixedObserver, policy_for_case
from ..vendor.attacker_observer import definitions_for
from .dataset import ACTIONS, complete_pairs, protocol_fingerprint, validate_rows
from .features import context_from_history
from .preparation import _groups, _validate_case


def _validated_cases(cases, config, queries):
    cases = deepcopy(list(cases))
    if not cases:
        raise ValueError('Collection needs at least one prepared case')
    ids, group_splits, base_splits = set(), {}, {}
    for case in cases:
        cid = case.get('case_id')
        if not isinstance(cid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', cid) or cid in ids:
            raise ValueError('Invalid or duplicate collection case ID')
        ids.add(cid)
        _validate_case(case)
        if case.get('split') not in ('train', 'dev', 'test'):
            raise ValueError('Collection requires an assigned train/dev/test split')
        if base_splits.setdefault(case['base_task_id'], case['split']) != case['split']:
            raise ValueError('Base task crosses collection splits')
        group = case.setdefault('group_id', case['base_task_id'])
        if not isinstance(group, str) or not group:
            raise ValueError('Invalid collection group identity')
        if group_splits.setdefault(group, case['split']) != case['split']:
            raise ValueError('Prepared group crosses collection splits')
        spec = case.get('goal_spec')
        if not isinstance(spec, dict) or spec.get('case_id') != cid:
            raise ValueError('Missing or foreign goal specification')
        validate_goal_spec(spec)
        if not spec['trigger_ids'] or spec.get('logic', 'all') != 'all':
            raise ValueError('Collection requires declared necessary conjunctive triggers')
        definitions = {p['id']: p for p in spec['predicates']}
        needed = set(spec['trigger_ids'])
        stack = list(needed)
        while stack:
            for dependency in definitions[stack.pop()].get('depends_on', []):
                if dependency not in needed:
                    needed.add(dependency)
                    stack.append(dependency)
        available = set(definitions_for(policy_for_case(case))) & set(queries)
        for predicate in spec['predicates']:
            quote = predicate.get('public_goal_quote')
            if quote and quote not in case['injection_task_goal']:
                raise ValueError('Predicate provenance differs from public goal')
        if any(definitions[pid]['query'] not in available for pid in needed):
            raise ValueError('Necessary trigger queries are unauthorized or disabled')
        # P0 is always freshly generated, regardless of any caller's cache.
        case.pop('p0', None)
    normalized_groups = _groups(cases)
    prompt_splits = {}
    for case in cases:
        group = normalized_groups[case['base_task_id']]
        if prompt_splits.setdefault(group, case['split']) != case['split']:
            raise ValueError('Identical normalized task prompts cross collection splits')
    if config['backend'] == 'agentdojo':
        validate_native_cases(cases, config['target'].get('benchmark_version', 'v1.2.2'))
    return cases


def _fingerprints(config, cases):
    paths = attacker_input_paths(config, cases)
    return {str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path): digest(path)
            for path in sorted(paths) if path.suffix != '.py'}


def collect_pairs(cases, config, output, *, repetitions=5, seed=20261029, target, attacker):
    """Collect one failed source history and K independent outcomes per candidate.

    Cases with successful/error sources or failed candidate generation remain in
    pairs.jsonl with their original status. Runtime missingness never filters a
    selected case and never becomes a negative outcome. Existing outputs cannot
    be resumed or overwritten by this function.
    """
    if type(repetitions) is not int or repetitions < 1 or type(seed) is not int:
        raise ValueError('Positive integer repetitions and integer seed required')
    config = deepcopy(config)
    if config.get('backend') not in ('fixture', 'agentdojo'):
        raise ValueError('Collection backend must be fixture or agentdojo')
    if isinstance(target, FixtureTarget) and config['backend'] != 'fixture':
        raise ValueError('Fixture execution cannot be labelled live AgentDojo')
    budget = config.get('post_failure_target_budget')
    Budget(budget)
    observer = FixedObserver(**config.get('observation', {}))
    protocol = dict(target=deepcopy(config.get('target')), attacker=deepcopy(config.get('attacker')),
                    observation=dict(queries=list(observer.queries)), repetitions=repetitions,
                    revision_mode='fixed_candidate')
    protocol_fingerprint(protocol)
    cases = _validated_cases(cases, config, observer.queries)
    kind = 'SYNTHETIC_FIXTURE' if config['backend'] == 'fixture' else 'LIVE_AGENTDOJO'
    manifest = dict(schema_version=1, kind='DECISION_PAIRED_COLLECTION', result_kind=kind,
        config=config, protocol=protocol, seed=seed, cases=cases,
        case_ids=[c['case_id'] for c in cases], inputs_sha256=_fingerprints(config, cases),
        independence='provisional_not_certified',
        sampling='Preselected observable cases; source Gold=False determines decision-training histories.',
        candidate_rule='Fresh P0; sole P1 generated from shared H1 before independent paired scoring.',
        execution_rule='K independent environment resets per candidate; shuffled arms each repetition; no success stopping.',
        feedback='Existing generator feedback retained; learned context excludes Gold and utility.',
        error_semantics='Started errors occupy null slots; no automatic retries or resumption.')
    output = (ROOT / output).resolve()
    store = RunStore(output, manifest)
    (output / 'pairs.jsonl').touch()
    runner = Runner(target, attacker, observer, {}, store,
                    post_failure_budget=budget, seed=seed)
    rows, current = [], None
    write_json(output / 'status.json', dict(status='running'))

    def finish_row(row):
        validate_rows(rows + [row])
        store.append('pairs', row)
        rows.append(deepcopy(row))

    def summary(status):
        _, accounting = complete_pairs(rows)
        initial = [e for e in runner.episodes if e.phase == 'initial']
        failed = [e for e in initial if e.gold is False and e.error is None]
        events = [json.loads(line) for line in (output / 'events.jsonl').read_text().splitlines()]
        started = Counter(e['event'] for e in events)
        return dict(schema_version=1, kind=kind, status=status, selected_cases=len(cases),
            **accounting,
            initial_successes=sum(e.gold is True for e in initial),
            initial_errors=sum(e.error is not None for e in initial),
            valid_initial_failures=len(failed),
            candidate_generation_failures=dict(Counter(
                r.get('error_phase', 'unknown') for r in rows if r['status'] == 'generation_error')),
            valid_failed_source_trigger_counts=dict(Counter(e.trigger for e in failed)),
            all_episode_trigger_counts=dict(Counter(e.trigger for e in runner.episodes)),
            target_started=started['TARGET_STARTED'], target_completed=started['TARGET_COMPLETED'],
            target_started_without_completion=started['TARGET_STARTED'] - started['TARGET_COMPLETED'],
            generation_started=started['GENERATION_STARTED'],
            generation_completed=started['GENERATION_COMPLETED'],
            physical_total_cost=runner.metrics()['physical_total_cost'],
            limitations=['Incomplete pairs are retained; model fitting uses complete pairs and reports missingness.',
                         'Source-success/error accounting is separate from paired conditional outcomes.',
                         'No independent-test certification or measured policy advantage follows from collection.'])

    try:
        for case in cases:
            current = dict(schema_version=1, kind=kind, pair_id=case['case_id'] + '/shared/000',
                base_task_id=case['base_task_id'], group_id=case['group_id'], case_id=case['case_id'],
                split=case['split'], status='generation_error', error_phase='P0', protocol=deepcopy(protocol))
            p0, generated = runner.generate(case, 'shared', None, None, 'P0')
            if p0 is None:
                current['error_type'] = generated.get('error_type', 'GENERATION_ERROR')
                finish_row(current)
                current = None
                continue
            current.update(status='initial_error', error_phase='H1')
            h1 = runner.execute(case, 'shared', p0, 'initial', 0)
            if h1.error is not None:
                current['error_type'] = h1.error
                finish_row(current)
                current = None
                continue
            if h1.gold is True:
                current.update(status='initial_success')
                current.pop('error_phase')
                finish_row(current)
                current = None
                continue
            current.update(status='generation_error', error_phase='P1',
                context=context_from_history(History.from_episodes([h1], Budget(budget))))
            p1, generated = runner.generate(case, 'pair', p0, h1, 'P1', history=[h1])
            if p1 is None:
                current['error_type'] = generated.get('error_type', 'GENERATION_ERROR')
                finish_row(current)
                current = None
                continue
            candidates = dict(Retry=p0, Revise=p1)
            current.pop('error_phase')
            current.update(status='paired', payloads={a: candidates[a].sha256 for a in ACTIONS},
                           outcomes={a: [None] * repetitions for a in ACTIONS})
            validate_rows([current])
            frozen_path = store.raw(case['case_id'] + '/frozen_pair.json', dict(
                row=deepcopy(current), source_episode_id=h1.episode_id,
                candidates={a: dict(id=candidates[a].id, text=candidates[a].text,
                                    sha256=candidates[a].sha256) for a in ACTIONS}))
            (output / frozen_path).chmod(0o444)
            store.append('events', dict(event='PAIR_FROZEN', pair_id=current['pair_id'],
                                       raw_path=frozen_path, payloads=current['payloads']))
            for repeat in range(repetitions):
                actions = list(ACTIONS)
                random.Random(f'{seed}:{case["case_id"]}:{repeat}').shuffle(actions)
                for action in actions:
                    episode = runner.execute(case, 'pair_' + action.lower(), candidates[action],
                                             'post_failure', repeat + 1)
                    current['outcomes'][action][repeat] = episode.gold
            finish_row(current)
            current = None
            write_json(output / 'summary.json', summary('running'))
        result = summary('completed')
        write_json(output / 'summary.json', result)
        write_json(output / 'status.json', dict(status='completed'))
        return result
    except BaseException as exc:
        if current is not None:
            current['interrupted'] = True
            current['interruption_type'] = type(exc).__name__
            finish_row(current)
        write_json(output / 'summary.json', summary('interrupted'))
        write_json(output / 'status.json', dict(status='interrupted', error_type=type(exc).__name__,
            recovery='Inspect durable events/raw and missing slots; use a new run directory, never silently resume.'))
        raise
