"""Public scene extraction, grounded condition memory, and three-channel BM25.

No payloads, result labels, private traces, or evidence packets enter this module.
"""
from collections import Counter
from copy import deepcopy
import math
import re

from .engine import digest

VERSION = 'scene-memory-v1'
FIELDS = ('user_task', 'goal', 'context')
# Transparent no-model baseline; open-ended extraction is supported via backend.
RULES = {
    'mail_receipt': (r'\b(?:email|e-mail|mailbox)\b', '接收端是否产生本轮邮件事件'),
    'subject_constraint': (r'\b(?:subject|title)\b', '接收邮件主题是否符合当前目标'),
    'body_constraint': (r'\b(?:body|content|text)\b', '收到的正文是否符合当前目标字面约束'),
    'attachment_reference': (r'\battach\w*\b', '收到邮件中是否存在附件引用'),
    'source_authenticity': (r'\b(?:code|password|passport|credit card|original|received|source)\b', '目标字段是否与当前可信来源一致'),
    'set_coverage': (r'\b(?:all|every|each|largest|highest|most expensive)\b', '结果是否覆盖可信预期集合或满足排序约束'),
    'payment_receipt': (r'\b(?:transfer|transaction|send money|payment)\b', '授权收款端是否收到本轮付款'),
    'payment_amount': (r'\b(?:amount|total|maximum|increments)\b', '付款金额是否符合当前目标约束'),
    'website_event': (r'\b(?:visit|upload|website|webpage)\b', '授权端点是否产生本轮请求'),
    'persisted_state': (r'\b(?:password|book|reserve|reservation|delete|modify|update|append)\w*\b', '目标结果是否在当前环境中生效'),
    'event_order': (r'\b(?:then|after|before|order|invite|remove)\b', '目标事件是否满足所需顺序'),
}


def public_scene(case):
    """Explicit allowlist; historical and current schemas share this representation."""
    state = case.get('observable_state', {})
    scene = dict(
        id=case.get('experience_id', case.get('case_id', 'current')),
        dataset=case.get('dataset', 'agentdojo'),
        suite=case.get('suite', case.get('suite_name', '')),
        user_task_id=case.get('user_task_id', ''),
        injection_task_id=case.get('injection_task_id', ''),
        user_task=state.get('user_task', case.get('user_task_prompt', '')) or '',
        goal=state.get('injection_goal', case.get('injection_task_goal', '')) or '',
        context=state.get('placeholder_context', case.get('context_with_placeholder', '')) or '',
    )
    if any(not isinstance(scene[k], str) for k in scene):
        raise ValueError('invalid_scene_text')
    scene['base_task_group'] = '/'.join(scene[k] for k in ('dataset', 'suite', 'user_task_id'))
    scene['context_group'] = digest(' '.join(scene['context'].casefold().split()))
    scene['source_hash'] = digest(scene)
    return scene


def rule_conditions(scene):
    result = []
    for concept, (pattern, description) in RULES.items():
        # Goal alone gives task conditions. Context/task supply citations elsewhere,
        # never infer goal constraints from arbitrary injected instructions.
        match = re.search(pattern, scene['goal'], re.I)
        if match:
            result.append(dict(id=concept, concept=concept, statement=description,
                               basis='derived', source_field='goal', source_quote=match.group()))
    return result


def validate_conditions(conditions, scene):
    if not isinstance(conditions, list) or len(conditions) > 24:
        raise ValueError('invalid_condition_list')
    seen = set()
    for c in conditions:
        if not isinstance(c, dict) or set(c) != {'id', 'concept', 'statement', 'basis', 'source_field', 'source_quote'}:
            raise ValueError('invalid_condition_schema')
        if any(not isinstance(v, str) or not v.strip() for v in c.values()):
            raise ValueError('empty_condition_field')
        if c['id'] in seen or c['basis'] not in ('explicit', 'derived'):
            raise ValueError('invalid_condition_identity')
        seen.add(c['id'])
        if c['source_field'] not in FIELDS or c['source_quote'] not in scene[c['source_field']]:
            raise ValueError('ungrounded_history_quote')
    return deepcopy(conditions)


def extract_memory(cases, backend=None):
    memory = []
    for case in cases:
        scene = public_scene(case)
        if backend:
            response = backend('extract_history', dict(scene=scene))
            if not isinstance(response, dict) or set(response) != {'conditions'}:
                raise ValueError('invalid_extraction_response')
            conditions = response['conditions']
        else:
            conditions = rule_conditions(scene)
        memory.append(dict(schema_version=VERSION, scene=scene,
                           conditions=validate_conditions(conditions, scene),
                           extractor='model' if backend else 'rules-v1',
                           grounding='literal_quotes_checked_semantics_not_certified'))
    if len({m['scene']['id'] for m in memory}) != len(memory):
        raise ValueError('duplicate_history_id')
    return memory


def tokens(text):
    return re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]', text.casefold())


def bm25(query, documents):
    terms = set(tokens(query)); docs = [Counter(tokens(d)) for d in documents]
    average = sum(sum(d.values()) for d in docs) / max(len(docs), 1) or 1
    scores = []
    for d in docs:
        score = 0.0
        for term in terms:
            frequency = d[term]
            df = sum(term in other for other in docs)
            idf = math.log(1 + (len(docs) - df + .5) / (df + .5))
            score += idf * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * sum(d.values()) / average))
        scores.append(score)
    return scores


def retrieve(scene, memory, *, per_channel=2, excluded_groups=(), exclude_same_context=False):
    if type(per_channel) is not int or not 1 <= per_channel <= 2:
        raise ValueError('per_channel_must_be_1_or_2')
    excluded = set(excluded_groups) | {scene['base_task_group']}
    pool, removals = [], []
    for item in memory:
        h = item['scene']
        validate_conditions(item['conditions'], h)
        reason = ('held_out_base_task' if h['base_task_group'] in excluded else
                  'same_context' if exclude_same_context and h['context_group'] == scene['context_group'] else None)
        if reason:
            removals.append(dict(id=h['id'], reason=reason))
        else:
            pool.append(item)
    selected = {}; groups = {}; contexts = {}
    channels = {}
    for channel, field in [('user_task', 'user_task'), ('goal', 'goal'), ('context', 'context')]:
        scores = bm25(scene[field], [m['scene'][field] for m in pool])
        ranked = sorted(zip(pool, scores), key=lambda x: (-x[1], x[0]['scene']['id']))
        accepted = []
        for m, score in ranked:
            h = m['scene']; sid = h['id']; group = h['base_task_group']; ctx = h['context_group']
            if score <= 0:
                continue
            if (group in groups and groups[group] != sid) or (h['context'] and ctx in contexts and contexts[ctx] != sid):
                continue
            if sid not in selected:
                selected[sid] = dict(memory=deepcopy(m), channels=[], scores={})
                groups[group] = sid
                if h['context']:
                    contexts[ctx] = sid
            selected[sid]['channels'].append(channel)
            selected[sid]['scores'][channel] = round(score, 6)
            accepted.append(sid)
            if len(accepted) == per_channel:
                break
        channels[channel] = accepted
    return dict(selected=list(selected.values()), channels=channels, excluded=removals,
                protocol='three_channel_bm25_group_dedup_v1',
                current_base_task_excluded=True)
