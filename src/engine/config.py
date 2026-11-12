"""Load public cases, validate identities, and freeze experiment data and resource inputs."""
from copy import deepcopy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import yaml
from ..evidence.ledger import validate_goal_spec
from ..evidence.observer import FixedObserver, policy_for_case, project_observable_goal_spec
from ..policies.registry import build_policies
from ..vendor.revision_checker import validate_constraints
from .budget import Budget


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_SCOPE = 'piminer_agentdojo_test'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def local(path):
    result = (ROOT / path).resolve()
    if not result.is_relative_to(ROOT):
        raise ValueError('Runtime inputs must be inside this project')
    return result


def load_config(path):
    config = yaml.safe_load(Path(path).read_text())
    if config.get('extends'):
        base = yaml.safe_load(local(config['extends']).read_text())
        base.update({k: v for k, v in config.items() if k != 'extends'})
        config = base
    for key in ('policies', 'observation'):
        if isinstance(config[key], str):
            config[key] = yaml.safe_load(local(config[key]).read_text())
    return config


def original_attacker(config):
    return config['backend'] == 'agentdojo' and config.get('attacker', {}).get('mode', 'original') == 'original'


def routed_attacker(config):
    return (config['backend'] == 'agentdojo'
            and config.get('attacker', {}).get('mode', 'original') in ('original', 'a2'))


def aligned_attacker(config):
    return (config['backend'] == 'agentdojo'
            and config.get('attacker', {}).get('mode') == 'piminer_aligned')


def initialization_bundle(config):
    from ..attacker.initialization import InitializationBundle
    path = config.get('attacker', {}).get('initialization_manifest')
    if not path:
        raise ValueError('PIMiner aligned mode requires initialization_manifest')
    return InitializationBundle(ROOT, path)


def strategy_paths(config, case, initializations=None):
    if aligned_attacker(config):
        bundle = initializations if initializations is not None else initialization_bundle(config)
        return bundle.for_case(case)['strategy_paths']
    if routed_attacker(config):
        # Router sees the entire original library, before any selection. Audit it all.
        paths = sorted((ROOT / 'strategy_library').glob('*_attack_strategy_*.md'))
        if not paths:
            paths = [ROOT / 'strategy_library/_TEMPLATE.md']
        return [str(p.relative_to(ROOT)) for p in paths]
    return case.get('strategy_paths', [])


def validate_benchmark_audit(config, cases):
    """Bind a public benchmark population to its frozen observable subset.

    Benchmark evaluation can include previously exposed tasks and repeated base
    tasks. It is deliberately separate from the independent held-out test split.
    """
    allow_unobservable = config.get('allow_unobservable_trigger') is True
    required = config.get('require_observable_trigger')
    if (config['backend'] != 'agentdojo'
            or (allow_unobservable and required is not False)
            or (not allow_unobservable and required is not True)
            or config.get('evaluation_scope') != BENCHMARK_SCOPE):
        raise ValueError('Benchmark split requires native AgentDojo, explicit Trigger observability settings, and an explicit benchmark scope')
    path = config.get('benchmark_audit')
    if not path:
        raise ValueError('Benchmark split requires a completed public population audit')
    audit = json.loads(local(path).read_text())
    ids = [c['case_id'] for c in cases]
    if (audit.get('status') != 'complete'
            or audit.get('evaluation_scope') != BENCHMARK_SCOPE
            or audit.get('independent_heldout') is not False
            or audit.get('dataset_sha256') != digest(local(config['dataset']))
            or audit.get('goal_specs_sha256') != digest(local(config['goal_specs']))
            or audit.get('selected_case_ids') != ids
            or audit.get('benchmark_version') != config['target']['benchmark_version']
            or not isinstance(audit.get('selection_rule'), str)
            or not audit['selection_rule'].strip()):
        raise ValueError('Missing, stale, or mismatched benchmark population audit')
    counts = [audit.get(k) for k in ('population_cases', 'selected_cases', 'excluded_cases')]
    if (any(type(n) is not int or n < 0 for n in counts)
            or counts[1] != len(cases) or counts[0] != counts[1] + counts[2]):
        raise ValueError('Benchmark population counts do not match the selected cases')
    if allow_unobservable:
        unobservable = [c['case_id'] for c in cases if not c['goal_spec']['trigger_ids']]
        if (audit.get('allow_unobservable_trigger') is not True
                or audit.get('unobservable_case_ids') != unobservable
                or audit.get('population_case_ids') != ids
                or counts != [len(cases), len(cases), 0]):
            raise ValueError('Unobservable benchmark cases require a matching explicit full-population audit')
    elif audit.get('allow_unobservable_trigger', False) is not False:
        raise ValueError('Unobservable benchmark audit requires an explicit configuration opt-in')


def load_cases(config):
    cases = load_jsonl(local(config['dataset']))
    initializations = initialization_bundle(config) if aligned_attacker(config) else None
    if initializations is not None and tuple(case.get('case_id') for case in cases) != initializations.case_ids:
        raise ValueError('Aligned initialization cases must match the full dataset in order')
    specs = load_jsonl(local(config['goal_specs']))
    if len({s['case_id'] for s in specs}) != len(specs):
        raise ValueError('Duplicate goal spec case ID')
    specs = {s['case_id']: s for s in specs}
    if not cases:
        raise ValueError('Dataset is empty. No independent held-out set has been certified; historical dev cases cannot be relabelled test.')
    if len({c['case_id'] for c in cases}) != len(cases):
        raise ValueError('Duplicate case ID')
    for case in cases:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', case['case_id']):
            raise ValueError('Unsafe case ID')
        required = ('suite_name', 'user_task_id', 'injection_task_id', 'user_task_prompt',
                    'injection_task_goal', 'context_with_placeholder', 'base_task_id')
        if any(not isinstance(case.get(k), str) or not case[k] for k in required):
            raise ValueError('Missing public task fields')
        expected_base = case['suite_name'] + '/' + case['user_task_id']
        if case['base_task_id'] != expected_base:
            raise ValueError('Base-task identity must match native suite/user task')
        if case.get('split') != config['split']:
            raise ValueError('Dataset split does not match experiment')
        if not isinstance(case['injection_candidates'], list) or not case['injection_candidates']:
            raise ValueError('Missing injection vectors')
        paths = strategy_paths(config, case, initializations)
        if not paths:
            raise ValueError('A common frozen strategy library is required')
        for path in paths:
            local(path).read_text()
        policy_for_case(case)
        validate_constraints(case.get('constraints', []), case)
        case['goal_spec'] = deepcopy(specs[case['case_id']])
        validate_goal_spec(case['goal_spec'])
        for p in case['goal_spec']['predicates']:
            quote = p.get('public_goal_quote')
            if quote and quote not in case['injection_task_goal']:
                raise ValueError('Predicate provenance does not match public goal')
        case['goal_spec'] = project_observable_goal_spec(
            case, FixedObserver(**config['observation']).queries)
        benchmark_requires_trigger = (config['split'] == 'benchmark'
                                      and config.get('allow_unobservable_trigger') is not True)
        if (config['split'] == 'test' or benchmark_requires_trigger
                or config.get('require_observable_trigger', False)) and not case['goal_spec']['trigger_ids']:
            raise ValueError('Main evidence evaluation requires an observable necessary Trigger: ' + case['case_id'])
    if config['split'] == 'benchmark':
        validate_benchmark_audit(config, cases)
    if config['split'] == 'test':
        if len({c['base_task_id'] for c in cases}) != len(cases):
            raise ValueError('Use one test case per base task or preregister a clustered analysis')
        dev = load_jsonl(ROOT / 'data/dev.jsonl')
        known = json.loads((ROOT / 'data/exposure_audit.json').read_text())['known_evaluated_base_tasks']
        dev_bases = {c['base_task_id'] for c in dev} | set(known)
        for case in cases:
            if case['base_task_id'] in dev_bases or case.get('provenance', {}).get('previously_evaluated') is not False:
                raise ValueError('Held-out case has prior evaluation exposure or unknown provenance')
            for p in strategy_paths(config, case, initializations):
                text = local(p).read_text()
                if case['user_task_prompt'] in text or case['context_with_placeholder'] in text:
                    raise ValueError('Test case overlaps frozen strategy examples')
        audit_path = config.get('heldout_audit')
        if not audit_path:
            raise ValueError('Test requires a completed provenance/strategy overlap audit path')
        audit = json.loads(local(audit_path).read_text())
        if (audit.get('dataset_sha256') != digest(local(config['dataset'])) or audit.get('status') != 'complete'
                or not audit.get('reviewer') or not audit.get('selection_rule')):
            raise ValueError('Missing or stale held-out audit')
    return cases


def validate(config):
    if config.get('backend') not in ('fixture', 'agentdojo') or config.get('split') not in ('dev', 'test', 'benchmark'):
        raise ValueError('Invalid backend/split')
    if config['backend'] == 'fixture' and config['split'] == 'test':
        raise ValueError('Synthetic fixtures cannot evaluate a held-out test')
    if config['split'] == 'benchmark' and config['backend'] != 'agentdojo':
        raise ValueError('Benchmark split requires the native AgentDojo backend')
    if ('allow_unobservable_trigger' in config
            and type(config['allow_unobservable_trigger']) is not bool):
        raise ValueError('allow_unobservable_trigger must be boolean')
    if config.get('allow_unobservable_trigger') is True and config['split'] != 'benchmark':
        raise ValueError('Unobservable Trigger opt-in applies only to the audited benchmark split')
    mode = config.get('attacker', {}).get('mode', 'original')
    if mode not in ('original', 'grounded', 'a2', 'piminer_aligned'):
        raise ValueError('Attacker mode must be original, grounded, a2 or piminer_aligned')
    options = config.get('attacker', {})
    if 'rolling_experience' in options and (mode != 'piminer_aligned'
                                          or type(options['rolling_experience']) is not bool):
        raise ValueError('rolling_experience must be boolean for the aligned attacker')
    profile = options.get('revision_profile', 'original')
    if profile not in ('original', 'calibrated') or ('revision_profile' in options and mode == 'grounded'):
        raise ValueError('revision_profile must be original or calibrated for a routed/aligned attacker')
    false_mode = options.get('false_revision_mode', 'original')
    if (false_mode not in ('original', 'grounded')
            or ('false_revision_mode' in options and mode != 'piminer_aligned')):
        raise ValueError('false_revision_mode must be original or grounded for piminer_aligned')
    if false_mode == 'grounded' and profile != 'original':
        raise ValueError('Evidence on F requires the original black-box profile on U')
    has_library, has_retrieval = bool(options.get('a2_library')), bool(options.get('a2_retrieval'))
    if (has_library != has_retrieval or
            (has_library and mode not in ('a2', 'piminer_aligned'))):
        raise ValueError('A2 resources require both a2_library and a2_retrieval in a2 or piminer_aligned mode')
    if mode == 'a2' and config['backend'] != 'agentdojo':
        raise ValueError('A2 requires the agentdojo backend; fixture results cannot validate A2')
    if mode == 'piminer_aligned' and config['backend'] != 'agentdojo':
        raise ValueError('PIMiner aligned mode requires the agentdojo backend')
    Budget(config['post_failure_target_budget'])
    if type(config.get('seed')) is not int:
        raise ValueError('Integer seed required')
    build_policies(config['policies'])
    FixedObserver(**config['observation'])
    cases = load_cases(config)
    if mode in ('a2', 'piminer_aligned'):
        attacker_input_paths(config, cases)
    from ..policies.learned import validate_runtime
    validate_runtime(config, cases)
    return cases


def environment_versions():
    versions = dict(python=sys.version.split()[0])
    for name in ('agentdojo', 'openai', 'pydantic', 'PyYAML'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def attacker_input_paths(config, cases):
    if aligned_attacker(config):
        bundle = initialization_bundle(config)
        if tuple(case['case_id'] for case in cases) != bundle.case_ids:
            raise ValueError('Aligned initialization cases must match the full dataset in order')
        for case in cases:
            bundle.for_case(case)
        paths = bundle.input_paths | {ROOT / 'src/vendor/attacker_role.txt',
                                     ROOT / 'data/assets_manifest.json'}
    else:
        paths = {local(path) for case in cases for path in strategy_paths(config, case)}
    if routed_attacker(config):
        paths.add(ROOT / 'src/vendor/attacker_role.txt')
        paths.add(ROOT / 'data/assets_manifest.json')
        paths |= {p for p in (ROOT / 'strategy_library').rglob('*') if p.is_file()}
    if (config.get('attacker', {}).get('mode') == 'a2'
            or config.get('attacker', {}).get('a2_library')
            or config.get('attacker', {}).get('a2_retrieval')):
        from ..attacker.a2 import A2Resources
        options = config['attacker']
        if not options.get('a2_library') or not options.get('a2_retrieval'):
            raise ValueError('A2 requires a2_library and a2_retrieval prepared resource paths')
        resources = A2Resources(ROOT, options['a2_library'], options['a2_retrieval'])
        for case in cases:
            resources.validate_case(case)
        paths |= resources.input_paths
    return paths


def data_fingerprints(hashes):
    """Ignore legacy Python source hashes; retain experimental data/resource hashes."""
    return {path: value for path, value in hashes.items() if Path(path).suffix != '.py'}


def without_source_hashes(manifest):
    """Copy a saved manifest for a new run without legacy source-code fingerprints."""
    result = dict(manifest)
    if 'inputs_sha256' in result:
        result['inputs_sha256'] = data_fingerprints(result['inputs_sha256'])
    for key in ('source_project', 'launcher_sha256', 'credential_helper_sha256',
                'verifier_sha256', 'native_verifier_sha256'):
        result.pop(key, None)
    return result


def fingerprint(config, cases):
    paths = {local(config['dataset']), local(config['goal_specs']), ROOT / 'data/exposure_audit.json'}
    if config.get('heldout_audit'):
        paths.add(local(config['heldout_audit']))
    if config.get('benchmark_audit'):
        paths.add(local(config['benchmark_audit']))
    if 'learned' in config['policies']:
        paths.add(local(config['policies']['learned']['model_path']))
    paths |= attacker_input_paths(config, cases)
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(paths) if p.suffix != '.py'}


def freeze(config):
    cases = validate(config)
    return dict(schema_version=1, config=config, inputs_sha256=fingerprint(config, cases),
                environment=environment_versions(), case_ids=[c['case_id'] for c in cases],
                result_kind='SYNTHETIC_FIXTURE' if config['backend']=='fixture' else 'LIVE_AGENTDOJO')


def check_frozen(manifest):
    cases = validate(manifest['config'])
    if fingerprint(manifest['config'], cases) != data_fingerprints(manifest['inputs_sha256']):
        raise ValueError('Frozen data/library bytes changed; create a new input snapshot')
    if environment_versions() != manifest['environment']:
        raise ValueError('Frozen runtime versions changed')
    return cases
