#!/usr/bin/env python3
"""One CLI for experiments and trainable decision data/models."""
import argparse
from datetime import datetime, timezone
import json
import sys
from pathlib import Path
from src.engine.config import without_source_hashes, ROOT, load_config, freeze, validate, check_frozen, digest
from src.engine.storage import RunStore, write_json
from src.engine.runner import Runner
from src.evidence.observer import FixedObserver
from src.policies.registry import build_policies


def adapters(config):
    if config['backend'] == 'fixture':
        from src.adapters.fixture import FixtureTarget, FixtureAttacker
        return FixtureTarget(), FixtureAttacker()
    from src.adapters.agentdojo import AgentDojoAdapter
    from src.attacker.piminer import PIMinerAdapter
    options = dict(config['attacker'])
    mode = options.pop('mode', 'original')
    if mode == 'original':
        from src.attacker.original import OriginalPIMinerAdapter
        attacker = OriginalPIMinerAdapter(ROOT, target_model=config['target']['model'], **options)
    elif mode == 'piminer_aligned':
        from src.attacker.aligned import PIMinerAlignedAdapter
        attacker = PIMinerAlignedAdapter(ROOT, target_model=config['target']['model'], **options)
    elif mode == 'a2':
        from src.attacker.a2 import A2PIMinerAdapter
        attacker = A2PIMinerAdapter(ROOT, target_model=config['target']['model'], **options)
    elif mode == 'grounded':
        attacker = PIMinerAdapter(ROOT, **options)
    else:
        raise ValueError('Unknown attacker mode: ' + mode)
    return AgentDojoAdapter(**config['target']), attacker


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'mismatch':
        from src.mismatch.cli import main as mismatch_main
        return mismatch_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == 'decision':
        from src.decision_learning.cli import main as decision_main
        return decision_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'freeze', 'preflight', 'run'):
        p = sub.add_parser(name)
        if name == 'run':
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument('--config')
            g.add_argument('--frozen')
        else:
            p.add_argument('--config', required=True)
        if name in ('run', 'freeze'):
            p.add_argument('--output', required=True)
        if name == 'preflight':
            p.add_argument('--live', action='store_true', help='Call both configured model endpoints')
    args = parser.parse_args()
    if getattr(args, 'frozen', None):
        manifest = without_source_hashes(json.loads(Path(args.frozen).read_text()))
        cases = check_frozen(manifest)
        config = manifest['config']
    else:
        config = load_config(args.config)
        cases = validate(config)
        manifest = freeze(config)
    if config['backend'] == 'agentdojo':
        from src.adapters.agentdojo import validate_native_cases
        native_preflight = validate_native_cases(cases, config['target']['benchmark_version'])
    else:
        native_preflight = dict(status='fixture_backend')
    if args.command == 'validate':
        print(json.dumps(dict(status='valid', cases=len(cases), backend=config['backend'], split=config['split'])))
        return
    if args.command == 'freeze':
        if Path(args.output).exists():
            raise ValueError('Freeze file exists; choose a new version')
        write_json(args.output, manifest)
        Path(args.output).chmod(0o444)
        print(args.output)
        return
    if args.command == 'preflight' and not args.live:
        print(json.dumps(dict(status='offline_valid', cases=len(cases), api_connectivity='not_checked',
                              native=native_preflight)))
        return
    if args.command == 'run' and config['split'] == 'test' and not args.frozen:
        raise ValueError('Held-out runs require --frozen')
    if args.command == 'run' and Path(args.output).exists():
        raise ValueError('Run directory exists; automatic resume/resampling is disabled')
    if args.command == 'run' and config['split'] == 'test':
        claim = ROOT / 'data/test_evaluations' / (digest(ROOT / config['dataset']) + '.json')
        if claim.exists():
            raise ValueError('This test dataset has already been claimed for evaluation')
    target, attacker = adapters(config)
    preflight = dict(native=native_preflight)
    if config['backend'] == 'agentdojo':
        # Fail before any dataset execution. This is a separate, reported cost.
        preflight.update(target=target.preflight(), attacker=attacker.preflight())
    if args.command == 'preflight':
        print(json.dumps(preflight))
        return
    manifest.update(created_at=datetime.now(timezone.utc).isoformat(), preflight=preflight)
    if config['split'] == 'test':
        # One evaluation claim per frozen dataset, not one per renamed output folder.
        claims = ROOT / 'data/test_evaluations'
        claims.mkdir(exist_ok=True)
        claim = claims / (digest(ROOT / config['dataset']) + '.json')
        with claim.open('x') as stream:
            json.dump(dict(output=str(Path(args.output).resolve()), created_at=manifest['created_at']), stream)
    store = RunStore(args.output, manifest)
    runner = Runner(target, attacker, FixedObserver(**config['observation']),
                    build_policies(config['policies']), store,
                    post_failure_budget=config['post_failure_target_budget'], seed=config['seed'])
    metrics = runner.run(cases)
    metrics['result_kind'] = manifest['result_kind']
    metrics['preflight_cost'] = preflight
    write_json(store.path / 'metrics.json', metrics)
    print(json.dumps(dict(output=str(store.path.resolve()), result_kind=manifest['result_kind'],
                          policies=list(metrics['policies'])), ensure_ascii=False))


if __name__ == '__main__':
    main()
