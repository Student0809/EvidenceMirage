"""Explicit task selection, separate from model-visible feedback."""
import json
from pathlib import Path

ROLES = {'M1': ('none', 'full', 'accurate_full_control'),
         'M2': ('mismatched_response', 'accurate_response'),
         'M3': ('mismatched_response', 'accurate_response')}


def load_selection(path):
    value = json.loads(Path(path).read_text())
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'tasks'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['tasks'], list)):
        raise ValueError('Selection requires schema_version=1 and tasks list')
    seen, selected = set(), []
    for row in value['tasks']:
        if not isinstance(row, dict) or set(row) != {'seed_id', 'mode', 'response_role', 'enabled'}:
            raise ValueError('Selection task fields: seed_id, mode, response_role, enabled')
        sid, mode, role = (row[k] for k in ('seed_id', 'mode', 'response_role'))
        if (sid not in [f'S{i:02d}' for i in range(1, 19)] or not isinstance(mode, str)
                or mode not in ROLES or role not in ROLES[mode] or type(row['enabled']) is not bool):
            raise ValueError('Invalid seed, mode, response_role or enabled value')
        identity = f'{sid}_{mode}:{role}'
        if identity in seen:
            raise ValueError('Duplicate selection: ' + identity)
        seen.add(identity)
        if row['enabled']:
            selected.append(identity)
    if not selected:
        raise ValueError('Selection contains no enabled tasks')
    return value, selected

