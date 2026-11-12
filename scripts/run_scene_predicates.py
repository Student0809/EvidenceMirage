#!/usr/bin/env python3
"""Build historical condition memory or run the complete predicate pipeline.

Default backend is a conservative no-model baseline. --backend model explicitly
uses the project's recorded, tool-free CLI transport and a persistent JSON cache.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.evidence.predicates.history import extract_memory
from src.evidence.predicates.pipeline import run_pipeline


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build-memory', 'run'])
    parser.add_argument('--backend', choices=['rules', 'model'], default='rules')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--deepseek', action='store_true', help='Use the project DeepSeek provider configuration')
    parser.add_argument('--deepseek-env', type=Path, help='Optional .env containing DEEPSEEK_API_KEY')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--library', type=Path, default=ROOT/'strategy_library_compact/experiences.jsonl')
    parser.add_argument('--memory', type=Path)
    parser.add_argument('--input', type=Path, help='JSON bundle: case, episode_id, policy, packets; optional excluded_groups')
    parser.add_argument('--without-history', action='store_true')
    parser.add_argument('--exclude-same-context', action='store_true')
    parser.add_argument('--output', type=Path, required=True, help='New output directory')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output must be a new directory')
    if args.command == 'run' and (args.memory is None or args.input is None):
        parser.error('run requires --memory and --input')
    if args.deepseek_env and not args.deepseek:
        parser.error('--deepseek-env requires --deepseek')
    if args.deepseek and args.backend != 'model':
        parser.error('--deepseek requires --backend model')
    provider = None
    if args.deepseek:
        from src.evidence.predicates.provider import configure_deepseek
        provider = configure_deepseek(args.deepseek_env)
        args.model = args.model or 'deepseek-flash[1m]'
    args.effort = args.effort or ('max' if args.deepseek else 'medium')
    backend = None
    if args.backend == 'model':
        if not args.model or not args.cache:
            parser.error('model backend requires explicit --model and --cache')
        from src.attacker.transport import RoleDecision
        from src.evidence.predicates.model import CachedJSONModel, SYSTEM
        decide = RoleDecision(args.model, args.effort, args.timeout, SYSTEM)
        backend = CachedJSONModel(decide, args.cache, dict(model=args.model, effort=args.effort, transport='RoleDecision', endpoint=os.environ.get('ANTHROPIC_BASE_URL', 'default')))
    args.output.mkdir(parents=True, exist_ok=False)
    if provider:
        (args.output/'provider_config.json').write_text(json.dumps(provider, indent=2)+'\n')
    try:
        if args.command == 'build-memory':
            memory = extract_memory(load_jsonl(args.library), backend)
            (args.output/'memory.jsonl').write_text(''.join(json.dumps(m, ensure_ascii=False)+'\n' for m in memory))
            result = dict(records=len(memory), conditions=sum(len(m['conditions']) for m in memory), backend=args.backend)
        else:
            bundle = json.loads(args.input.read_text())
            result = run_pipeline(bundle['case'], load_jsonl(args.memory), bundle['episode_id'],
                                  bundle['policy'], bundle['packets'], backend=backend,
                                  excluded_groups=bundle.get('excluded_groups', []),
                                  exclude_same_context=args.exclude_same_context,
                                  use_history=not args.without_history)
            for name in ('retrieval', 'semantic_predicates', 'compilation', 'feedback', 'manifest'):
                (args.output/(name+'.json')).write_text(json.dumps(result[name], ensure_ascii=False, indent=2)+'\n')
        (args.output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    except Exception as exc:
        (args.output/'error.json').write_text(json.dumps({'error_type':type(exc).__name__})+'\n')
        raise
    finally:
        if backend:
            (args.output/'model_cache_calls.json').write_text(json.dumps(backend.calls, indent=2)+'\n')
            (args.output/'provider_calls.json').write_text(json.dumps(decide.calls, ensure_ascii=False, indent=2, default=str)+'\n')
    print(json.dumps(dict(output=str(args.output.resolve()), backend=args.backend)))


if __name__ == '__main__':
    main()
