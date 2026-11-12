"""Opt-in A2 v2 retrieval; v1 archival resources and selector stay unchanged.

This module uses only public situations. It does not inspect model outcomes,
private traces, attacker payloads, or evidence-controller observations.
"""
import hashlib
import math
import re
import unicodedata

PROTOCOL = 'minipim-a2-v2'
SELECTOR = 'tier_preserving_distinct_group_situation_v1'
FIELD_POLICY = 'task_and_slot_windows_v1'
MARKER = re.compile(r'\{INJECTION:[^}]*\}')


def normalized_situation(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def select_cases_v2(cases, *, strategy_id, scenario, task_type, current_group, scores, k=2):
    """Fill precise tiers first, excluding repeated groups and situations."""
    if k < 1 or not task_type:
        raise ValueError('Positive k and a reviewed task_type are required')
    available = [c for c in cases if c['strategy_id'] == strategy_id
                 and c['group'] != list(current_group)]
    if any(not c.get('task_type') or not isinstance(c.get('situation'), str)
           or not c['situation'].strip() for c in available):
        raise ValueError('Candidate task_type and nonempty situation are required')
    if any(not math.isfinite(scores[c['case_id']]) for c in available):
        raise ValueError('Finite candidate scores are required')
    priority = {'success': 0, 'recovered': 1, 'unknown': 2, 'failure': 3}
    tiers = [('same_scenario_and_type', [c for c in available if c['scenario'] == scenario
                                       and c['task_type'] == task_type]),
             ('same_type_any_scenario', [c for c in available if c['task_type'] == task_type]),
             ('any_case_of_strategy', available)]
    selected, selected_tiers, skipped, visited, groups, situations = [], {}, [], set(), set(), set()
    for tier, pool in tiers:
        for case in sorted(pool, key=lambda c: (priority[c['outcome_label']], -scores[c['case_id']], c['case_id'])):
            cid = case['case_id']
            if cid in visited:
                continue
            visited.add(cid)
            group, situation = tuple(case['group']), normalized_situation(case['situation'])
            if group in groups or situation in situations:
                skipped.append(cid)
                continue
            selected.append(case)
            selected_tiers[cid] = tier
            groups.add(group)
            situations.add(situation)
            if len(selected) == k:
                break
        if len(selected) == k:
            break
    return selected, dict(selector=SELECTOR, fallback_tier=tier, eligible_count=len(pool),
        requested=k, loaded=len(selected), shortfall=max(0, k - len(selected)),
        selected_tiers=selected_tiers, duplicates_skipped=skipped,
        recovered_available=any(c['outcome_label'] == 'recovered' for c in available),
        known_outcomes=sum(c['outcome_label'] != 'unknown' for c in available),
        scores={c['case_id']: float(scores[c['case_id']]) for c in selected})


def _length(tokenizer, text):
    return len(tokenizer(text, add_special_tokens=True, truncation=False)['input_ids'])


def _offsets(tokenizer, text):
    return tokenizer(text, add_special_tokens=False, truncation=False,
                     return_offsets_mapping=True)['offset_mapping']


def _head_tail(text, tokenizer, budget):
    if _length(tokenizer, text) <= budget:
        return text
    offsets = _offsets(tokenizer, text)
    count = max(1, budget - _length(tokenizer, '') - 4)
    # Every candidate is checked after retokenization: token boundary cuts can
    # introduce new wordpieces, and the separator itself consumes tokens.
    while count:
        left, right = (count + 1) // 2, count // 2
        value = text[:offsets[left - 1][1]] + '\n...\n' + (text[offsets[-right][0]:] if right else '')
        if _length(tokenizer, value) <= budget:
            return value
        count -= 1
    raise ValueError('Token budget cannot fit a text excerpt')


def _slot_window(context, match, tokenizer, budget, offsets):
    if _length(tokenizer, match.group()) > budget:
        raise ValueError('Injection marker exceeds context token budget')
    intersect = [i for i, (a, b) in enumerate(offsets) if b > match.start() and a < match.end()]
    if not intersect:
        raise ValueError('Tokenizer did not retain injection marker offsets')
    first, last = intersect[0], intersect[-1] + 1
    spare = max(0, budget - _length(tokenizer, '') - (last - first) - 4)
    start = max(0, first - spare // 2)
    end = min(len(offsets), last + spare - (first - start))
    start = max(0, start - max(0, spare - (first - start) - (end - last)))
    while True:
        value = context[offsets[start][0]:offsets[end - 1][1]]
        if match.group() in value and _length(tokenizer, value) <= budget:
            return value
        if first - start >= end - last and start < first:
            start += 1
        elif end > last:
            end -= 1
        else:
            raise ValueError('Unable to retain the complete injection marker')


def build_embedding_fields(user_task, context, tokenizer, *, task_budget=192, context_budget=320):
    """Return separately encoded task and slot excerpts, plus truncation audit.

Each literal slot receives its own bounded window. Contexts without a marker
retain their beginning and end. Only retrieval views are shortened; generated
prompts still receive unchanged methods and raw examples.
"""
    if not 16 <= task_budget <= 512 or not 16 <= context_budget <= 512:
        raise ValueError('Field token budgets must be between 16 and 512')
    markers = list(MARKER.finditer(context))
    task = _head_tail(user_task, tokenizer, task_budget)
    if markers:
        offsets = _offsets(tokenizer, context)
        windows = [_slot_window(context, marker, tokenizer, context_budget, offsets) for marker in markers]
        windows = list(dict.fromkeys(windows))
    else:
        windows = [_head_tail(context, tokenizer, context_budget)]
    situation = user_task + '\n\n' + context
    full_offsets = _offsets(tokenizer, situation)
    legacy_budget = 512 - _length(tokenizer, '')
    legacy_end = max((b for _, b in full_offsets[:legacy_budget]), default=0)
    shift = len(user_task) + 2
    retained = sum(any(marker.group() in text for text in windows) for marker in markers)
    audit = dict(task_original_tokens=_length(tokenizer, user_task), task_tokens=_length(tokenizer, task),
        context_original_tokens=_length(tokenizer, context), context_window_tokens=[_length(tokenizer, w) for w in windows],
        context_marker_count=len(markers), retained_marker_count=retained,
        legacy_situation_tokens=_length(tokenizer, situation), legacy_retained_end=legacy_end,
        legacy_retained_marker_count=sum(marker.end() + shift <= legacy_end for marker in markers),
        legacy_all_markers_retained=all(marker.end() + shift <= legacy_end for marker in markers),
        input_sha256=hashlib.sha256(situation.encode()).hexdigest(),
        field_sha256={name: [hashlib.sha256(text.encode()).hexdigest() for text in texts]
                      for name, texts in {'task': [task], 'context': windows}.items()})
    return {'task': [task], 'context': windows}, audit
