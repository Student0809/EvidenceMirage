"""Exact MiniPIM A2 selection and lossless partition helpers.

Upstream snapshots and hashes: strategy_library_a2/provenance.json.
No model or filesystem dependency is required to select frozen cases.
"""

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


def partition(document, converter):
    """Partition exact source bytes; fenced headings never start new examples."""
    segments, examples = [], []
    offset = 0
    for block in document['source_blocks']:
        text = block['markdown']
        if (block['heading'] or '').startswith('In-context examples'):
            starts = [item for item in converter.headings(text, 3) if item[3].startswith('Example ')]
            prefix_end = starts[0][0] if starts else len(text)
            segments.append(('other', text[:prefix_end]))
            for i, (start, _, _, _) in enumerate(starts):
                stop = starts[i + 1][0] if i + 1 < len(starts) else len(text)
                raw = text[start:stop]
                line = converter.restore_markdown(document)[:offset + start].count('\n') + 1
                segments.append(('case', raw))
                examples.append((line, raw))
        else:
            segments.append(('method', text))
        offset += len(text)
    if ''.join(text for _, text in segments) != converter.restore_markdown(document):
        raise ValueError(f"Source partition lost content: {document['id']}")
    if len(examples) != len(document['examples']):
        raise ValueError(f"Example boundary mismatch: {document['id']}")
    for (line, _), example in zip(examples, document['examples']):
        if line != example['source_line']:
            raise ValueError(f"Example source line mismatch: {document['id']}, {line}")
    return segments, examples
