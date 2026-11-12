"""Strict paired labels and group-disjoint split validation, without imputation."""
import json
import math
import re
from collections import Counter

from .features import validate_context

ACTIONS = ('Retry', 'Revise')
KINDS = ('LIVE_AGENTDOJO', 'SYNTHETIC_FIXTURE')
SPLITS = ('train', 'dev', 'test')
STATUSES = ('paired', 'initial_success', 'initial_error', 'generation_error')
PROTOCOL_FIELDS = frozenset({'target', 'attacker', 'observation', 'repetitions', 'revision_mode'})


def protocol_fingerprint(protocol):
    if not isinstance(protocol, dict) or not PROTOCOL_FIELDS <= set(protocol):
        raise ValueError('Protocol is missing target/attacker/observation/repetitions/revision_mode')
    if type(protocol['repetitions']) is not int or protocol['repetitions'] < 1:
        raise ValueError('Protocol repetitions must be a positive integer')
    if not isinstance(protocol['revision_mode'], str) or not protocol['revision_mode']:
        raise ValueError('Protocol revision_mode is required')
    for field in ('target', 'attacker', 'observation'):
        if not isinstance(protocol[field], (dict, str)) or not protocol[field]:
            raise ValueError('Protocol ' + field + ' must be a nonempty config or fingerprint')
    try:
        return json.dumps(protocol, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError('Protocol must be finite JSON') from error


def validate_rows(rows):
    rows = list(rows)
    pair_ids, group_splits, duplicate_group_splits, protocols, kinds = set(), {}, {}, set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Each data row must be an object')
        if type(row.get('schema_version')) is not int or row['schema_version'] != 1:
            raise ValueError('Unknown decision row schema')
        for field in ('pair_id', 'base_task_id'):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError('Missing ' + field)
        if row['pair_id'] in pair_ids:
            raise ValueError('Duplicate pair_id: ' + row['pair_id'])
        pair_ids.add(row['pair_id'])
        if row.get('kind') not in KINDS or row.get('split') not in SPLITS or row.get('status') not in STATUSES:
            raise ValueError('Unknown kind, split or status')
        group_id = row.get('group_id', row['base_task_id'])
        if not isinstance(group_id, str) or not group_id:
            raise ValueError('group_id must be a nonempty string when supplied')
        previous_group = duplicate_group_splits.setdefault(group_id, row['split'])
        if previous_group != row['split']:
            raise ValueError('Normalized-prompt group crosses data splits: ' + group_id)
        previous = group_splits.setdefault(row['base_task_id'], row['split'])
        if previous != row['split']:
            raise ValueError('Base task crosses data splits: ' + row['base_task_id'])
        protocols.add(protocol_fingerprint(row.get('protocol')))
        kinds.add(row['kind'])
        if row.get('context') is not None:
            validate_context(row['context'])
        if row['status'] == 'paired':
            if row.get('context') is None:
                raise ValueError('Paired row needs a legal context')
            outcomes, payloads = row.get('outcomes'), row.get('payloads')
            if not isinstance(outcomes, dict) or set(outcomes) != set(ACTIONS):
                raise ValueError('Paired outcomes need both action arms')
            if not isinstance(payloads, dict) or set(payloads) != set(ACTIONS):
                raise ValueError('Paired row needs both fixed payload hashes')
            for action in ACTIONS:
                if (not isinstance(payloads[action], str)
                        or not re.fullmatch('[0-9a-f]{64}', payloads[action])):
                    raise ValueError('Payload must be a SHA-256 digest')
                arm = outcomes[action]
                if not isinstance(arm, list) or any(v is not None and type(v) is not bool for v in arm):
                    raise ValueError('Outcome labels must be booleans or null, never truthy values')
                if len(arm) > row['protocol']['repetitions']:
                    raise ValueError('More outcomes than preregistered repetitions')
            if len(outcomes['Retry']) != len(outcomes['Revise']):
                raise ValueError('Paired arms must have equal slots; use null for failed executions')
    if len(protocols) > 1:
        raise ValueError('Mixed protocols are not a single training/evaluation dataset')
    if len(kinds) > 1:
        raise ValueError('Synthetic fixtures and live AgentDojo rows cannot be mixed')
    return rows


def is_complete_pair(row):
    return (row['status'] == 'paired'
            and all(len(row['outcomes'][a]) == row['protocol']['repetitions']
                    and all(type(v) is bool for v in row['outcomes'][a]) for a in ACTIONS))


def complete_pairs(rows, split=None):
    rows = validate_rows(rows)
    if split is not None and split not in SPLITS:
        raise ValueError('Unknown split')
    selected = [r for r in rows if split is None or r['split'] == split]
    paired = [r for r in selected if r['status'] == 'paired']
    complete = [r for r in paired if is_complete_pair(r)]
    planned = sum(2 * r['protocol']['repetitions'] for r in paired)
    observed = sum(sum(type(v) is bool for v in r['outcomes'][a]) for r in paired for a in ACTIONS)
    accounting = dict(total_rows=len(selected), status_counts=dict(Counter(r['status'] for r in selected)),
                      paired_rows=len(paired), complete_pairs=len(complete),
                      incomplete_pairs=len(paired) - len(complete),
                      excluded_rows=len(selected) - len(complete),
                      planned_paired_labels=planned, observed_paired_labels=observed,
                      missing_paired_labels=planned - observed,
                      complete_base_tasks=len({r['base_task_id'] for r in complete}),
                      complete_groups=len({r.get('group_id', r['base_task_id']) for r in complete}))
    return complete, accounting
