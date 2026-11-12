"""Lossless A/A2/A3 resources for the experiment before Brief generation.

No case text is cleaned here. Source offsets, registry IDs and hashes survive.
A3 uses the compressed strategy library from reconstruct-strategy-library/.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
import re

from scripts.library_token_audit import load_converter, partition

TASK_TYPES = {
    'information_retrieval': 'Read, find, summarize, calculate or report existing information; no requested persistent change.',
    'message_operation': 'Send, reply, forward, publish or change delivery/read state of messages or emails.',
    'member_management': 'Invite, add or remove people, or change membership or access permissions.',
    'calendar_operation': 'Create, change or cancel calendar events.',
    'booking_operation': 'Make, change or cancel travel, hotel, restaurant or transport bookings.',
    'financial_operation': 'Make or schedule payments or transfers, or change financial records.',
    'file_operation': 'Create, edit, delete, upload or share files or documents.',
    'software_operation': 'Inspect, implement, execute or configure software or system operations.',
    'other': 'The main user intent cannot be assigned confidently to one of the above.',
}


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TextMeter:
    def __init__(self, path):
        from tokenizers import Tokenizer
        self.path = Path(path).resolve()
        self.tokenizer = Tokenizer.from_file(str(self.path))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()

    def count(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def request(self, system, payload, schema):
        stdin = json.dumps(payload, ensure_ascii=False)
        schema_text = json.dumps(schema) if schema is not None else ''
        return {'system_tokens': self.count(system), 'stdin_json_tokens': self.count(stdin),
                'schema_json_tokens': self.count(schema_text),
                'system_plus_stdin_tokens': self.count(system) + self.count(stdin),
                'visible_input_tokens': self.count(system) + self.count(stdin) + self.count(schema_text),
                'stdin_sha256': hashlib.sha256(stdin.encode()).hexdigest()}


def load_sources(root):
    root = Path(root)
    converter = load_converter(root)
    index = json.loads((root / 'strategy_library_json/index.json').read_text())
    taxonomy = json.loads((root / 'strategy_library_compact/index.json').read_text())
    families = {entry['strategy_id']: entry['mechanism'] for entry in taxonomy['cards']}
    strategies, cases, hashes = {}, [], {}
    for entry in index['entries']:
        if entry['kind'] != 'strategy':
            continue
        source = root / 'strategy_library' / (entry['id'] + '.md')
        text = source.read_bytes().decode('utf-8')
        if file_hash(source) != entry['source_sha256']:
            raise ValueError(f'Source changed since registry export: {source}')
        document = converter.convert_document(text, source.name)
        exported = json.loads((root / 'strategy_library_json' / entry['file']).read_text())
        if document != exported:
            raise ValueError(f'Stale structured export: {entry["id"]}')
        segments, raw_cases = partition(document, converter)
        strategies[entry['id']] = {'id': entry['id'], 'family': families[entry['id']],
                                  'source': str(source.relative_to(root)), 'text': text,
                                  'method': ''.join(value for kind, value in segments if kind == 'method')}
        hashes[str(source.relative_to(root))] = file_hash(source)
        for ordinal, ((line, raw), example) in enumerate(zip(raw_cases, document['examples']), 1):
            benchmark = example['benchmark_case']
            dataset = 'agentdojo' if entry['id'].startswith('agentdojo_') else 'ipi_arena'
            cases.append({'case_id': f'{entry["id"]}::example-{ordinal:03d}', 'strategy_id': entry['id'],
                          'dataset': dataset, 'scenario': benchmark['suite'],
                          'group': [dataset, benchmark['suite'], benchmark['user_task_id']],
                          'task_type': None, 'outcome_label': 'unknown',
                          'outcome_evidence': 'Source has narrative result; no structured round-level verification.',
                          'user_task': example['user_task'] or '',
                          'situation': (example['user_task'] or '') + '\n\n' + (example['placeholder_context'] or ''),
                          'raw': raw, 'source': str(source.relative_to(root)), 'source_line': line,
                          'source_sha256': entry['source_sha256']})
    if len(strategies) != index['strategy_count'] or len(cases) != index['example_count']:
        raise ValueError('Registry counts do not match source resources')
    return strategies, cases, hashes


def smoke_tasks(root, strategies, seed=42):
    """One group per suite, absent even from unscoped source task-ID mentions."""
    seen = set()
    for strategy in strategies.values():
        seen.update(re.findall(r'\buser_task_\d+\b', strategy['text']))
    path = Path(root) / 'data/agentdojo/agentdojo_injection_steps.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rng, selected = random.Random(seed), []
    for scenario in ('banking', 'slack', 'travel', 'workspace'):
        pool = [row for row in rows if row['suite_name'] == scenario and row['user_task_id'] not in seen]
        groups = sorted({row['user_task_id'] for row in pool})
        if not groups:
            raise ValueError(f'No source-unreferenced smoke group in {scenario}')
        identifier = rng.choice(groups)
        instances = sorted((row for row in pool if row['user_task_id'] == identifier),
                           key=lambda row: (row['injection_task_id'], row['step_index']))
        row = rng.choice(instances)
        selected.append({'key': f'{scenario}:{row["user_task_id"]}:{row["injection_task_id"]}:{row["step_index"]}',
                         'group': ['agentdojo', scenario, identifier], 'row': row,
                         'task_type': None, 'leakage_check': 'ID absent from every original strategy document'})
    return selected


def select_cases(cases, *, strategy_id, scenario, task_type, current_group, scores, k=2):
    """Exclude groups before fallback; never duplicate/pad/truncate raw cases."""
    if k < 1:
        raise ValueError('k must be positive')
    if not task_type:
        raise ValueError('A reviewed or recorded task_type is required')
    available = [case for case in cases if case['strategy_id'] == strategy_id
                 and case['group'] != list(current_group)]
    if any(not case.get('task_type') for case in available):
        raise ValueError('Unlabelled candidate cases cannot silently bypass type filtering')
    pool = [case for case in available if case['scenario'] == scenario and case['task_type'] == task_type]
    tier = 'same_scenario_and_type'
    if len(pool) < k:
        pool = [case for case in available if case['task_type'] == task_type]
        tier = 'same_type_any_scenario'
    if len(pool) < k:
        pool, tier = available, 'any_case_of_strategy'
    priority = {'success': 0, 'recovered': 1, 'unknown': 2, 'failure': 3}
    ranked = sorted(pool, key=lambda case: (priority[case['outcome_label']], -scores[case['case_id']], case['case_id']))
    selected = ranked[:k]
    recovered = sorted((case for case in pool if case['outcome_label'] == 'recovered'),
                       key=lambda case: (-scores[case['case_id']], case['case_id']))
    if recovered and recovered[0] not in selected:
        selected[-1:] = [recovered[0]]
    selected.sort(key=lambda case: (-scores[case['case_id']], case['case_id']))
    return selected, {'fallback_tier': tier, 'eligible_count': len(pool), 'requested': k,
                      'loaded': len(selected), 'shortfall': max(0, k - len(selected)),
                      'recovered_available': bool(recovered),
                      'known_outcomes': sum(case['outcome_label'] != 'unknown' for case in pool),
                      'scores': {case['case_id']: float(scores[case['case_id']]) for case in selected}}


COMPRESSED_LIBRARY = Path(__file__).resolve().parents[1] / 'reconstruct-strategy-library'

MECHANISM_TO_COMPRESSED = {
    'forged_chat_turn': 'forged_chat_turn_injection.md',
    'fabricated_procedure_gate': 'fabricated_procedure_gate.md',
    'appended_output_directive': 'appended_output_directive.md',
    'entity_data_poisoning': 'entity_data_poisoning.md',
    'authenticated_principal_voice_forge': 'authenticated_principal_voice_forge.md',
    'authority_shed_docstring': 'authority_shed_docstring.md',
    'false_history_forge': 'false_history_forge.md',
}


def load_compressed_card(family):
    filename = MECHANISM_TO_COMPRESSED.get(family)
    if not filename:
        raise ValueError(f'No compressed card for mechanism: {family}')
    path = COMPRESSED_LIBRARY / 'strategies' / filename
    if not path.exists():
        raise ValueError(f'Compressed strategy file missing: {path}')
    return path.read_text(encoding='utf-8')


def render_variant(strategies, selected, variant):
    if variant == 'A':
        return {identifier: strategy['text'] for identifier, strategy in strategies.items()}
    if variant == 'A2':
        return {identifier: strategy['method'] + '\n\n## Selected original examples\n\n'
                + '\n\n'.join(case['raw'] for case in selected.get(identifier, []))
                for identifier, strategy in strategies.items()}
    if variant == 'A3':
        result = {}
        for identifier, strategy in strategies.items():
            compressed = load_compressed_card(strategy['family'])
            cases = selected.get(identifier, [])
            if cases:
                result[identifier] = compressed + '\n\n## Selected original examples\n\n' \
                    + '\n\n'.join(case['raw'] for case in cases)
            else:
                result[identifier] = compressed
        return result
    raise ValueError(f'Unknown variant: {variant}')
