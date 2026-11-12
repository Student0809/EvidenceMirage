#!/usr/bin/env python3
"""Evaluate hand-written V1 predicates against trusted frozen observer packets."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.evidence.predicates import PredicateEngine
from src.evidence.predicates.engine import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='JSON bundle: episode_id, policy, references, predicates, packets')
    parser.add_argument('--output', type=Path, required=True, help='New run directory (must not exist)')
    args = parser.parse_args()
    bundle = json.loads(args.input.read_text())
    engine = PredicateEngine(bundle['episode_id'], bundle['policy'], bundle.get('references'))
    for packet in bundle['packets']:
        engine.add_packet(packet)
    result = engine.evaluate(bundle['predicates'])
    args.output.mkdir(parents=True, exist_ok=False)
    outputs = {'feedback.json': result,
               'input_manifest.json': {'input': str(args.input.resolve()), 'input_hash': digest(bundle),
                                       'stage': 'M1', 'model_calls': 0},
               'predicates.json': bundle['predicates'],
               'judgments.json': result['judgments'],
               'rejections.json': result['rejected_candidates']}
    for filename, value in outputs.items():
        (args.output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'judgments': len(result['judgments']), 'rejections': len(result['rejected_candidates']),
                      'output': str(args.output.resolve())}))


if __name__ == '__main__':
    main()
