"""Prepare paired data, collect, train, evaluate, and exercise the learned policy."""
import argparse
import json
from pathlib import Path

from ..engine.config import ROOT, load_config, load_jsonl
from ..engine.storage import write_json


def _new_output(path, value):
    path = Path(path)
    if path.exists():
        raise FileExistsError('Output exists; select a new version: ' + str(path))
    write_json(path, value)


def _prepared_cases(directory, splits, maximum):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    specs = load_jsonl(directory / 'goal_specs.jsonl')
    if len({s['case_id'] for s in specs}) != len(specs):
        raise ValueError('Duplicate prepared goal specification')
    by_id = {s['case_id']: s for s in specs}
    cases = []
    if len(splits) != len(set(splits)):
        raise ValueError('Duplicate selected split')
    for split in splits:
        part = load_jsonl(directory / (split + '.jsonl'))
        if any(c['split'] != split for c in part):
            raise ValueError('Prepared split mismatch')
        cases.extend(part)
    if maximum is not None:
        if maximum < 1:
            raise ValueError('max-cases must be positive')
        cases = cases[:maximum]
    if not cases:
        raise ValueError('No prepared cases selected')
    for case in cases:
        case['goal_spec'] = by_id[case['case_id']]
    return cases, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    schema = commands.add_parser('schema')
    schema.add_argument('--output', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--output', required=True)
    prepare.add_argument('--pool', default='data/native_pool.jsonl')
    prepare.add_argument('--reference-cases', default='data/dev.jsonl')
    prepare.add_argument('--goal-specs', default='data/goal_specs.jsonl')
    prepare.add_argument('--exposure', default='data/exposure_audit.json')
    prepare.add_argument('--extra-exposure', action='append', default=[])
    prepare.add_argument('--seed', type=int, default=20261029)
    prepare.add_argument('--dev-fraction', type=float, default=0.2)
    prepare.add_argument('--test-fraction', type=float, default=0.2)
    prepare.add_argument('--max-per-base', type=int, default=1)
    collect = commands.add_parser('collect')
    collect.add_argument('--prepared', required=True)
    collect.add_argument('--config', default=str(ROOT / 'configs/experiment.yaml'))
    collect.add_argument('--output', required=True)
    collect.add_argument('--splits', nargs='+', choices=('train', 'dev', 'test'), default=['train', 'dev'])
    collect.add_argument('--repetitions', type=int, default=5)
    collect.add_argument('--seed', type=int, default=20261029)
    collect.add_argument('--max-cases', type=int)
    collect.add_argument('--dry-run', action='store_true')
    train = commands.add_parser('train')
    train.add_argument('--data', nargs='+', required=True)
    train.add_argument('--output', required=True)
    train.add_argument('--l2', type=float, default=0.01)
    train.add_argument('--steps', type=int, default=800)
    train.add_argument('--learning-rate', type=float, default=0.1)
    evaluate = commands.add_parser('evaluate')
    evaluate.add_argument('--model', required=True)
    evaluate.add_argument('--data', nargs='+', required=True)
    evaluate.add_argument('--split', choices=('dev', 'test'), default='dev')
    evaluate.add_argument('--output', required=True)
    evaluate.add_argument('--revision-penalty', type=float, default=0.0)
    evaluate.add_argument('--tie-margin', type=float, default=0.0)
    evaluate.add_argument('--fallback-action', choices=('Retry', 'Revise'), default='Retry')
    smoke = commands.add_parser('smoke')
    smoke.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if args.command == 'schema':
        _new_output(args.output, dict(schema_version=1, artifact='DECISION_DATA_SCHEMA',
                    splits=['train', 'dev', 'test'], actions=['Retry', 'Revise'],
                    label='independent next-execution official Gold for fixed candidates',
                    model='paired_logistic_v1', feature_boundary='completed legal evidence only',
                    note='Task records alone do not supply paired labels.'))
        result = dict(output=args.output)
    elif args.command == 'prepare':
        from .preparation import prepare_data
        prepared = prepare_data(args.output, pool_path=args.pool,
                              reference_cases_path=args.reference_cases, goal_specs_path=args.goal_specs,
                              exposure_path=args.exposure, extra_exposure_paths=args.extra_exposure,
                              seed=args.seed, dev_fraction=args.dev_fraction,
                              test_fraction=args.test_fraction, max_per_base=args.max_per_base)
        result = dict(output=args.output, counts=prepared['counts'], splits=prepared['splits'],
                      excluded_reasons=prepared['excluded_reasons'], warnings=prepared['warnings'],
                      independence=prepared['independence'])
    elif args.command == 'collect':
        from .collection import collect_pairs
        cases, preparation = _prepared_cases(args.prepared, args.splits, args.max_cases)
        if args.repetitions < 1:
            raise ValueError('repetitions must be positive')
        config = load_config(args.config)
        config['decision_preparation'] = preparation
        if args.dry_run:
            result = dict(status='plan_only', cases=len(cases), repetitions=args.repetitions,
                          maximum_target_executions=len(cases) * (1 + 2 * args.repetitions),
                          maximum_candidate_generations=2 * len(cases),
                          router_calls_additional=config.get('attacker', {}).get('mode', 'original') in ('original', 'a2'),
                          api_calls=0, output_not_created=args.output)
        else:
            if Path(args.output).exists():
                raise FileExistsError('Collection output exists')
            from main import adapters
            target, attacker = adapters(config)
            result = collect_pairs(cases, config, args.output, repetitions=args.repetitions,
                                   seed=args.seed, target=target, attacker=attacker)
    elif args.command == 'train':
        from .model import fit_model
        if Path(args.output).exists():
            raise FileExistsError('Model output exists')
        rows = [row for path in args.data for row in load_jsonl(path)]
        model = fit_model(rows, l2=args.l2, steps=args.steps, learning_rate=args.learning_rate)
        _new_output(args.output, model)
        result = dict(output=args.output, kind=model['kind'], training=model['training_summary'])
    elif args.command == 'evaluate':
        from .model import evaluate_model
        model = json.loads(Path(args.model).read_text())
        rows = [row for path in args.data for row in load_jsonl(path)]
        result = evaluate_model(model, rows, split=args.split, revision_penalty=args.revision_penalty,
                                tie_margin=args.tie_margin, fallback_action=args.fallback_action)
        _new_output(args.output, result)
    else:
        from .smoke import run_smoke
        result = run_smoke(args.output)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
