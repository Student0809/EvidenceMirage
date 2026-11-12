"""A small, explicit feature boundary for attacker-visible decision context.

IDs, payload text, oracle labels and Episode.stage never enter this module's
vectors. Unknown evidence stays unknown; omission is not negative evidence.
"""
import math

from ..evidence.ledger import EVENT_QUERIES

QUERY_NAMES = tuple(sorted(EVENT_QUERIES | {'received_attachment_counts'}))
STATES = ('T', 'F', 'U')
SIGNALS = ('Reject', 'Partial', 'Ignored')
CONTEXT_KEYS = frozenset({'response_signal', 'trigger', 'predicates', 'attempts',
                          'remaining_budget', 'spent_budget',
                          'same_payload_trigger_counts', 'observation_error'})
FEATURE_VERSION = 1
FEATURE_NAMES = (
    ('bias',)
    + tuple('response:' + s for s in SIGNALS)
    + tuple('trigger:' + s for s in STATES)
    + ('log_attempts', 'log_remaining_budget', 'log_spent_budget', 'remaining_fraction')
    + tuple('same_payload_fraction:' + s for s in STATES)
    + ('observation_error',)
    + tuple('evidence:' + q + ':' + s for q in QUERY_NAMES for s in ('present',) + STATES)
)


def validate_context(context):
    """Reject extra fields rather than accidentally learning privileged metadata."""
    if not isinstance(context, dict) or set(context) != CONTEXT_KEYS:
        raise ValueError('Context must contain exactly the legal feature fields')
    if context['response_signal'] not in SIGNALS or context['trigger'] not in STATES:
        raise ValueError('Unknown response signal or trigger')
    for name in ('attempts', 'remaining_budget', 'spent_budget'):
        value = context[name]
        if type(value) is not int or value < (1 if name == 'attempts' else 0):
            raise ValueError('Invalid context count: ' + name)
    if type(context['observation_error']) is not bool:
        raise ValueError('observation_error must be boolean')
    counts = context['same_payload_trigger_counts']
    if (not isinstance(counts, dict) or set(counts) != set(STATES)
            or any(type(n) is not int or n < 0 for n in counts.values())
            or sum(counts.values()) != context['attempts']):
        raise ValueError('Trigger history counts must sum to attempts')
    if context['spent_budget'] < context['attempts'] - 1:
        raise ValueError('Post-failure spent budget cannot be smaller than attempts minus shared H1')
    if not isinstance(context['predicates'], list):
        raise ValueError('Predicates must be a list')
    for record in context['predicates']:
        if (not isinstance(record, dict) or set(record) != {'query', 'status', 'is_trigger'}
                or record['query'] not in QUERY_NAMES or record['status'] not in STATES
                or record['is_trigger'] is not True):
            raise ValueError('Only registered trigger predicates may enter context')
    return context


def context_from_history(history):
    if not history.completed:
        raise ValueError('A decision requires a completed history')
    last = history.completed[-1]
    attempts = tuple(e for e in history.completed if e.payload_sha256 == last.payload_sha256)
    context = dict(
        response_signal=last.response_signal, trigger=last.trigger,
        predicates=[dict(query=p.query, status=p.status, is_trigger=True)
                    for p in last.evidence if p.is_trigger],
        attempts=len(attempts), remaining_budget=history.remaining_target_budget,
        spent_budget=history.spent_target_budget,
        same_payload_trigger_counts={state: sum(e.trigger == state for e in attempts)
                                     for state in STATES},
        observation_error=last.observation_error,
    )
    return validate_context(context)


def feature_vector(context):
    validate_context(context)
    attempts = context['attempts']
    remaining, spent = context['remaining_budget'], context['spent_budget']
    values = [1.0]
    values.extend(float(context['response_signal'] == s) for s in SIGNALS)
    values.extend(float(context['trigger'] == s) for s in STATES)
    values.extend((math.log1p(attempts), math.log1p(remaining), math.log1p(spent),
                   remaining / max(1, remaining + spent)))
    values.extend(context['same_payload_trigger_counts'][s] / attempts for s in STATES)
    values.append(float(context['observation_error']))
    for query in QUERY_NAMES:
        observed = [p for p in context['predicates'] if p['query'] == query]
        values.append(float(bool(observed)))
        values.extend(sum(p['status'] == s for p in observed) / max(1, len(observed))
                      for s in STATES)
    return values
