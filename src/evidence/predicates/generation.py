"""Semantic discovery and conservative compilation into the M1 engine schema."""
from copy import deepcopy
import re

from .contracts import CONTRACTS, OPERATORS
from .engine import PredicateEngine
from .history import rule_conditions

UNBOUND_CONCEPTS = {'source_authenticity', 'set_coverage', 'persisted_state', 'event_order'}


def _literal(goal, kind):
    # Deliberately narrow. Broken/mixed quotes or instructions embedded in the
    # captured value are evidence requirements, never silently repaired.
    noun = 'subject' if kind == 'subject_constraint' else 'body'
    match = re.search(r'\b' + noun + r'\s+(?:should be|must be|is|of)\s*([\'"])([^\n]+?)\1', goal, re.I)
    if not match or re.search(r'\b(?:should|must|email|and then)\b', match.group(2), re.I):
        return None
    return match.group(2)


def rule_discovery(scene, retrieved):
    """No-model baseline, not a claim of learned or novel condition discovery."""
    current = rule_conditions(scene)
    historical = {}
    for item in retrieved['selected']:
        memory = item['memory']
        for c in memory['conditions']:
            historical.setdefault(c['concept'], []).append(dict(example_id=memory['scene']['id'], condition_id=c['id']))
    predicates = []
    for c in current[:12]:
        concept = c['concept']; binding = None
        if concept == 'mail_receipt':
            binding = dict(query_id='email_received', operator='exists_event', field=None, expected_quote=None)
        elif concept == 'payment_receipt':
            binding = dict(query_id='payment_received', operator='exists_event', field=None, expected_quote=None)
        elif concept == 'website_event':
            binding = dict(query_id='website_visited', operator='exists_event', field=None, expected_quote=None)
        elif concept in ('subject_constraint', 'body_constraint'):
            value = _literal(scene['goal'], concept)
            if value is not None:
                binding = dict(query_id='received_email_subjects' if concept == 'subject_constraint' else 'received_email_bodies',
                               operator='exists_equal', field=None, expected_quote=value)
        predicates.append(dict(id='p_' + concept, concept=concept, statement=c['statement'],
                               current_quote=c['source_quote'], history_refs=historical.get(concept, []),
                               evidence_need=c['statement'], binding=binding))
    return predicates


def discover(scene, retrieved, backend=None):
    if backend:
        # No query responses, private traces or outcome labels are sent to models.
        response = backend('discover', dict(current=scene, historical_conditions=[
            dict(example_id=item['memory']['scene']['id'], scene=item['memory']['scene'],
                 conditions=item['memory']['conditions'], retrieval_channels=item['channels'])
            for item in retrieved['selected']], contracts=CONTRACTS, operators=sorted(OPERATORS)))
        if not isinstance(response, dict) or set(response) != {'predicates'}:
            raise ValueError('invalid_discovery_response')
        predicates = response['predicates']
    else:
        predicates = rule_discovery(scene, retrieved)
    if not isinstance(predicates, list) or len(predicates) > 12:
        raise ValueError('invalid_semantic_predicate_batch')
    return predicates


def compile_predicates(semantic, scene, retrieved, episode_id, policy):
    """Validate provenance; expected values are always exact current-goal quotes.

This certifies schema/provenance, not semantic equivalence of an LLM binding.
Review/evaluation of binding faithfulness is still required.
"""
    history_ids = {(item['memory']['scene']['id'], c['id']) for item in retrieved['selected']
                   for c in item['memory']['conditions']}
    candidates, references, rejections, requirements, lineage = [], {}, [], [], []
    seen = set()
    fields = {'id', 'concept', 'statement', 'current_quote', 'history_refs', 'evidence_need', 'binding'}
    for item in semantic:
        pid = item.get('id') if isinstance(item, dict) else None
        try:
            if not isinstance(item, dict) or set(item) != fields:
                raise ValueError('invalid_semantic_schema')
            for k in ('id', 'concept', 'statement', 'current_quote', 'evidence_need'):
                if not isinstance(item[k], str) or not item[k].strip():
                    raise ValueError('invalid_semantic_field')
            if not re.fullmatch(r'[A-Za-z0-9_]{1,80}', pid) or pid in seen:
                raise ValueError('invalid_or_duplicate_semantic_id')
            seen.add(pid)
            if item['current_quote'] not in scene['goal']:
                raise ValueError('ungrounded_current_quote')
            refs = item['history_refs']
            if not isinstance(refs, list):
                raise ValueError('invalid_history_refs')
            for ref in refs:
                if (not isinstance(ref, dict) or set(ref) != {'example_id', 'condition_id'}
                        or not all(isinstance(x, str) for x in ref.values())
                        or (ref['example_id'], ref['condition_id']) not in history_ids):
                    raise ValueError('unretrieved_history_reference')
            binding = item['binding']
            if binding is None:
                requirements.append(dict(predicate_id=pid, statement=item['statement'],
                                         reason_code='no_faithful_binding', evidence_need=item['evidence_need'],
                                         verdict='U', history_refs=refs))
                lineage.append(deepcopy(item))
                continue
            if item['concept'] in UNBOUND_CONCEPTS:
                raise ValueError('semantic_weakening_forbidden')
            if not isinstance(binding, dict) or set(binding) != {'query_id', 'operator', 'field', 'expected_quote'}:
                raise ValueError('invalid_binding_schema')
            p = dict(schema_version='predicate-v1', id=pid, statement=item['statement'],
                     role='diagnostic', query_id=binding['query_id'], operator=binding['operator'],
                     scope=dict(episode_id=episode_id, resource_ref='authorized_policy', domain='rollout_events'),
                     history_refs=sorted({ref['example_id'] for ref in refs}))
            quote = binding['expected_quote']
            if p['operator'] == 'exists_event':
                if quote is not None:
                    raise ValueError('unexpected_literal')
            else:
                if not isinstance(quote, str) or not quote or quote not in scene['goal']:
                    raise ValueError('expected_value_not_current_goal')
                p['expected_ref'] = 'current_goal.' + pid
                references[p['expected_ref']] = quote
            if binding['field'] is not None:
                p['field'] = binding['field']
            validator = PredicateEngine(episode_id, policy, references)
            validation = validator.evaluate([p])
            if validation['rejected_candidates']:
                raise ValueError(validation['rejected_candidates'][0]['reason_code'])
            # Unknown interfaces/permissions remain visible U requirements, not facts.
            candidates.append(p)
            lineage.append(deepcopy(item))
        except (TypeError, ValueError) as exc:
            rejections.append(dict(predicate_id=pid, reason_code=str(exc)))
    return dict(candidates=candidates, references=references, rejections=rejections,
                evidence_requirements=requirements, lineage=lineage,
                semantic_equivalence='not_certified_by_literal_quote_checks')
