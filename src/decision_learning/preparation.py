"""Offline, outcome-independent preparation of observable native task groups.

This constructs a project split, not an official AgentDojo training split. A
prepared test split remains provisional and does not satisfy formal E1 audits.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import json
import math
from pathlib import Path
import random
import re

from ..evidence.ledger import validate_goal_spec
from ..evidence.observer import DEFAULT_QUERIES, policy_for_case
from ..vendor.attacker_observer import definitions_for

ROOT = Path(__file__).resolve().parents[2]
LATER_EXPOSURE = (ROOT.parent / 'PIMiner/eval_results/subgoal_comparison/'
                  'retry_prediction_greeting36_20261028/selection.json')
PUBLIC_FIELDS = ('case_id', 'suite_name', 'user_task_id', 'injection_task_id',
                 'base_task_id', 'user_task_prompt', 'injection_task_goal',
                 'context_with_placeholder', 'injection_candidates', 'step_index',
                 'resource_grants', 'constraints')


def _path(value):
    return (ROOT / value).resolve()


def _read_rows(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    ids = []
    for row in rows:
        cid = row.get('case_id') if isinstance(row, dict) else None
        if not isinstance(cid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', cid):
            raise ValueError('Invalid case ID in ' + str(path))
        ids.append(cid)
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate case ID in ' + str(path))
    return rows


def _validate_case(case):
    required = ('suite_name', 'user_task_id', 'injection_task_id', 'base_task_id',
                'user_task_prompt', 'injection_task_goal', 'context_with_placeholder')
    if any(not isinstance(case.get(k), str) or not case[k].strip() for k in required):
        raise ValueError('Missing public task fields: ' + case['case_id'])
    if case['base_task_id'] != case['suite_name'] + '/' + case['user_task_id']:
        raise ValueError('Invalid base-task identity: ' + case['case_id'])
    vectors = case.get('injection_candidates')
    if (not isinstance(vectors, list) or not vectors
            or any(not isinstance(v, str) or not v for v in vectors)
            or len(vectors) != len(set(vectors))):
        raise ValueError('Invalid injection vectors: ' + case['case_id'])
    policy_for_case(case)


def _family(case):
    return tuple(case[k] for k in ('suite_name', 'injection_task_id', 'injection_task_goal'))


def _exposure_groups(value):
    """Read recorded identities only; never inspect outcome labels for selection."""
    if isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            groups = value
        else:
            groups = []
            for row in value:
                if not isinstance(row, dict):
                    raise ValueError('Invalid exposure row')
                group = row.get('base_task_id') or row.get('group_id')
                sample = row.get('sample', {})
                if not group and isinstance(sample, dict):
                    group = sample.get('base_task_id')
                    if not group and sample.get('suite_name') and sample.get('user_task_id'):
                        group = sample['suite_name'] + '/' + sample['user_task_id']
                if not group:
                    raise ValueError('Missing exposure base-task identity')
                groups.append(group)
    elif isinstance(value, dict):
        for key in ('known_evaluated_base_tasks', 'known_exposed_base_tasks', 'cases', 'selected'):
            if key in value:
                return _exposure_groups(value[key])
        raise ValueError('Unsupported exposure source schema')
    else:
        raise ValueError('Invalid exposure source')
    if any(not isinstance(g, str) or not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_-]+', g)
           for g in groups):
        raise ValueError('Invalid exposure base-task identity')
    return set(groups)


def _exposures(reference_cases, exposure_path, extra_paths):
    sources, warnings = [], []

    def add(path, value, kind):
        groups = sorted(_exposure_groups(value))
        sources.append(dict(path=str(path), kind=kind, base_task_ids=groups))

    add(exposure_path, json.loads(exposure_path.read_text()), 'recorded_audit')
    # Defining method behavior using reference cases is itself development exposure.
    add('reference_cases', reference_cases, 'method_development_references')
    extras = [_path(p) for p in extra_paths]
    if LATER_EXPOSURE.exists() and LATER_EXPOSURE not in extras:
        add(LATER_EXPOSURE, json.loads(LATER_EXPOSURE.read_text()), 'later_selected_campaign')
    elif not LATER_EXPOSURE.exists():
        warnings.append('Later greeting36 exposure source unavailable; independent history not certified.')
    for path in extras:
        add(path, json.loads(path.read_text()), 'explicit_extra')
    # Existing native runs count conservatively even if a case never finished.
    for path in sorted((ROOT / 'runs').rglob('manifest.json')):
        manifest = json.loads(path.read_text())
        config = manifest.get('config', {})
        if config.get('backend') != 'agentdojo':
            continue
        if 'cases' in manifest:
            # Collection snapshots are the actual selected tasks. config.dataset
            # may still identify an inherited development config, not this run.
            rows, ids = manifest['cases'], manifest.get('case_ids')
            if not isinstance(rows, list) or not rows or not isinstance(ids, list):
                raise ValueError('Invalid native run case snapshot: ' + str(path))
            snapshot_ids = []
            for row in rows:
                cid = row.get('case_id') if isinstance(row, dict) else None
                if not isinstance(cid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', cid):
                    raise ValueError('Invalid native run snapshot case ID: ' + str(path))
                _validate_case(row)
                snapshot_ids.append(cid)
            if (len(snapshot_ids) != len(set(snapshot_ids))
                    or any(not isinstance(cid, str) for cid in ids)
                    or len(ids) != len(set(ids)) or set(snapshot_ids) != set(ids)):
                raise ValueError('Native run snapshot identities differ from case_ids: ' + str(path))
            add(path, rows, 'local_native_run_snapshot')
            continue
        dataset = config.get('dataset')
        if not dataset or not _path(dataset).is_file():
            warnings.append('Cannot resolve native run dataset: ' + str(path))
            continue
        rows = _read_rows(_path(dataset))
        ids = manifest.get('case_ids')
        if ids is not None:
            rows = [r for r in rows if r['case_id'] in ids]
            if len(rows) != len(set(ids)):
                warnings.append('Native run identities differ from current dataset: ' + str(path))
        add(path, rows, 'local_native_run_dataset')
    known = {g for source in sources for g in source['base_task_ids']}
    return known, sources, warnings


def _groups(cases):
    """Union base tasks with equal normalized public prompts before splitting."""
    parent = {r['base_task_id']: r['base_task_id'] for r in cases}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    prompts = {}
    for case in cases:
        key = ' '.join(case['user_task_prompt'].casefold().split())
        base = case['base_task_id']
        if key in prompts:
            a, b = find(base), find(prompts[key])
            parent[max(a, b)] = min(a, b)
        else:
            prompts[key] = base
    return {base: find(base) for base in sorted(parent)}


def _distribution(rows):
    return dict(cases=len(rows), base_tasks=len({r['base_task_id'] for r in rows}),
                groups=len({r['group_id'] for r in rows}),
                suites=dict(sorted(Counter(r['suite_name'] for r in rows).items())),
                goal_families=dict(sorted(Counter(r['suite_name'] + '/' + r['injection_task_id']
                                                  for r in rows).items())))


def prepare_data(output, *, pool_path='data/native_pool.jsonl',
                 reference_cases_path='data/dev.jsonl', goal_specs_path='data/goal_specs.jsonl',
                 exposure_path='data/exposure_audit.json', extra_exposure_paths=(),
                 seed=20261029, dev_fraction=0.2, test_fraction=0.2, max_per_base=1):
    """Write a new immutable preparation directory without making model calls.

    Fractions apply to unexposed groups with floor rounding. Known exposed
    groups are train-only; all remaining groups enter train. Limited eligible
    coverage can therefore produce an empty dev or test split, reported openly.
    """
    if type(seed) is not int or type(max_per_base) is not int or max_per_base < 1:
        raise ValueError('Integer seed and positive integer max_per_base required')
    if (any(type(f) not in (int, float) or not math.isfinite(f) or not 0 <= f < 1
            for f in (dev_fraction, test_fraction)) or dev_fraction + test_fraction >= 1):
        raise ValueError('Fractions must be finite, nonnegative, and sum to less than one')
    output = _path(output)
    if output.exists():
        raise FileExistsError(output)
    pool_path, reference_cases_path, goal_specs_path, exposure_path = map(
        _path, (pool_path, reference_cases_path, goal_specs_path, exposure_path))
    pool, references, specs = (_read_rows(p) for p in
                              (pool_path, reference_cases_path, goal_specs_path))
    for row in pool + references:
        _validate_case(row)
    refs = {r['case_id']: r for r in references}
    templates, template_refs = {}, defaultdict(list)
    for spec in specs:
        if spec['case_id'] not in refs:
            raise ValueError('Goal spec has no public reference case: ' + spec['case_id'])
        validate_goal_spec(spec)
        if spec.get('logic', 'all') != 'all':
            raise ValueError('Only conjunction goal specifications are supported')
        case = refs[spec['case_id']]
        for predicate in spec['predicates']:
            quote = predicate.get('public_goal_quote')
            if quote and quote not in case['injection_task_goal']:
                raise ValueError('Predicate source differs from public goal')
        key = _family(case)
        semantic = {k: deepcopy(v) for k, v in spec.items() if k != 'case_id'}
        if key in templates and templates[key] != semantic:
            raise ValueError('Conflicting exact-family goal specifications')
        templates[key] = semantic
        template_refs[key].append(spec['case_id'])
    exposed, exposure_sources, warnings = _exposures(references, exposure_path, extra_exposure_paths)
    group_for_base = _groups(pool + references)
    exposed_groups = {group_for_base.get(base, base) for base in exposed}
    eligible, excluded = [], []
    case_specs = {}
    for source in sorted(pool, key=lambda r: r['case_id']):
        case = {k: deepcopy(source[k]) for k in PUBLIC_FIELDS if k in source}
        spec = templates.get(_family(case))
        reason = None
        if spec is None:
            reason = 'no_exact_goal_spec'
        elif not spec['trigger_ids']:
            reason = 'no_observable_necessary_trigger'
        else:
            predicates = {p['id']: p for p in spec['predicates']}
            needed = set(spec['trigger_ids'])
            stack = list(needed)
            while stack:
                for dep in predicates[stack.pop()].get('depends_on', []):
                    if dep not in needed:
                        needed.add(dep)
                        stack.append(dep)
            authorized = set(definitions_for(policy_for_case(case))) & set(DEFAULT_QUERIES)
            if any(predicates[pid]['query'] not in authorized for pid in needed):
                reason = 'necessary_query_not_authorized'
        if reason:
            excluded.append(dict(case_id=case['case_id'], base_task_id=case['base_task_id'],
                                 reason=reason))
            continue
        case['group_id'] = group_for_base[case['base_task_id']]
        case['provenance'] = dict(source='native_pool_public_projection',
            source_case_id=case['case_id'], source_metadata=deepcopy(source.get('provenance', {})),
            goal_spec_references=sorted(template_refs[_family(case)]),
            known_prior_exposure=case['group_id'] in exposed_groups,
            independence='provisional_not_certified')
        case_specs[case['case_id']] = dict(deepcopy(spec), case_id=case['case_id'])
        eligible.append(case)
    per_base = defaultdict(list)
    for case in eligible:
        per_base[case['base_task_id']].append(case)
    selected = []
    for base in sorted(per_base):
        rows = per_base[base]
        random.Random(f'{seed}:selection:{base}').shuffle(rows)
        selected.extend(rows[:max_per_base])
        excluded.extend(dict(case_id=r['case_id'], base_task_id=base, reason='max_per_base')
                        for r in rows[max_per_base:])
    if not selected:
        raise ValueError('No eligible observable cases; no training split can be prepared')
    groups = sorted({r['group_id'] for r in selected})
    unexposed = [g for g in groups if g not in exposed_groups]
    random.Random(f'{seed}:group_split').shuffle(unexposed)
    n_dev, n_test = math.floor(len(unexposed) * dev_fraction), math.floor(len(unexposed) * test_fraction)
    assignment = {g: 'train' for g in groups}
    assignment.update({g: 'dev' for g in unexposed[:n_dev]})
    assignment.update({g: 'test' for g in unexposed[n_dev:n_dev + n_test]})
    partitions = {s: [] for s in ('train', 'dev', 'test')}
    for case in sorted(selected, key=lambda r: r['case_id']):
        case['split'] = assignment[case['group_id']]
        partitions[case['split']].append(case)
    if not partitions['train']:
        raise ValueError('Preparation requires a nonempty train split')
    for split in ('dev', 'test'):
        if not partitions[split]:
            warnings.append(split + ' split is empty; fraction floor or exposure leaves too few groups.')
    manifest = dict(schema_version=1, kind='AGENTDOJO_DECISION_PREPARATION',
        split_origin='project_constructed_not_official_supervised_split',
        independence='provisional_not_certified', seed=seed, max_per_base=max_per_base,
        dev_fraction=dev_fraction, test_fraction=test_fraction,
        split_rule='Known exposure train-only; floor fractions of unexposed prompt/base groups.',
        inputs=dict(pool=str(pool_path), reference_cases=str(reference_cases_path),
                    goal_specs=str(goal_specs_path), exposure=str(exposure_path)),
        counts=dict(pool=len(pool), eligible_before_cap=len(eligible), selected=len(selected),
                    excluded=len(excluded)),
        eligible_distribution=_distribution(eligible),
        splits={s: _distribution(rows) for s, rows in partitions.items()},
        groups=dict(sorted(assignment.items())),
        known_exposed_base_tasks=sorted(exposed), exposure_sources=exposure_sources,
        excluded_reasons=dict(sorted(Counter(r['reason'] for r in excluded).items())),
        warnings=warnings,
        limitations=['No outcome-based filtering; runtime failures remain in the collection accounting.',
                     'Base-task holdout shares native environments and injection-goal families.',
                     'Known exposure scan is conservative and cannot certify all historical use.',
                     'Formal E1 provenance/strategy overlap certification remains separate.'])
    output.mkdir(parents=True, exist_ok=False)

    def write_rows(name, rows):
        (output / name).write_text(''.join(json.dumps(r, sort_keys=True, ensure_ascii=False) + '\n'
                                         for r in rows))

    for split, rows in partitions.items():
        write_rows(split + '.jsonl', rows)
    write_rows('goal_specs.jsonl', [case_specs[r['case_id']] for r in sorted(selected, key=lambda r: r['case_id'])])
    write_rows('excluded.jsonl', sorted(excluded, key=lambda r: r['case_id']))
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + '\n')
    return manifest
