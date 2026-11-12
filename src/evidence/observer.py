"""A fixed query interface for all five arms; no active query selection."""
from copy import deepcopy
from ..vendor import attacker_observer as native
from .ledger import validate_goal_spec


DEFAULT_QUERIES = ('email_received', 'received_attachment_counts', 'website_visited',
                   'website_uploads', 'payment_received', 'slack_direct_messages',
                   'slack_membership_events', 'slack_channel_messages')


def policy_for_case(case):
    wrapped = native.make_policy([dict(case, sample_index=0)],
                                 resource_grants={'0': case.get('resource_grants', {})})
    return native.policy_for({native.KEY: wrapped}, 0)


class FixedObserver:
    def __init__(self, queries=DEFAULT_QUERIES):
        if len(set(queries)) != len(queries):
            raise ValueError('Duplicate fixed queries')
        known = set(DEFAULT_QUERIES)
        if set(queries) - known:
            raise ValueError('Unknown or privileged query')
        self.queries = tuple(queries)

    def collect(self, episode_id, case, view):
        if view is None:
            return ()
        policy = policy_for_case(case)
        native.validate_view(view, policy)
        authorized = native.definitions_for(policy)
        packets = []
        for query in self.queries:
            if query not in authorized:
                continue
            complete = query in view.get('event_coverage', {}).get('query_ids', [])
            packet = dict(episode_id=episode_id, query_id=query,
                                source=view['source'], policy=view['policy'],
                                scope='authorized_observed_events',
                                completeness=(native.COMPLETE_EVENT_CONTRACT if complete
                                              else 'returned_event_fields_only'),
                                response=native.answer(view, policy, query), charged_cost=1)
            if complete:
                packet['event_coverage'] = deepcopy(view['event_coverage'])
            packets.append(packet)
        return tuple(packets)


def project_observable_goal_spec(case, queries=DEFAULT_QUERIES):
    """Freeze necessary diagnostics reachable under this case's legal channels.

    This is a static capability projection, before executing any payload. An
    authorized enabled query remains in Trigger even if its runtime answer is
    missing or unknown. Predicates outside Trigger remain available for audit;
    they must never turn an observed local behavior into unknown merely because
    a separate private action is inaccessible.

    Dependencies are part of the diagnostic definition. If a required dependency
    is unreachable, exclude that diagnostic rather than silently dropping the
    dependency and changing what it means.
    """
    observer = FixedObserver(queries)
    spec = deepcopy(case['goal_spec'])
    validate_goal_spec(spec)
    available = set(native.definitions_for(policy_for_case(case))) & set(observer.queries)
    definitions = {p['id']: p for p in spec['predicates']}
    reachable = {}

    def can_observe(pid):
        if pid not in reachable:
            predicate = definitions[pid]
            reachable[pid] = (predicate['query'] in available and
                              all(can_observe(dep) for dep in predicate.get('depends_on', [])))
        return reachable[pid]

    excluded = [pid for pid in spec['trigger_ids'] if not can_observe(pid)]
    spec['trigger_ids'] = [pid for pid in spec['trigger_ids'] if can_observe(pid)]
    spec['excluded_unobservable_trigger_ids'] = list(dict.fromkeys(
        spec.get('excluded_unobservable_trigger_ids', []) + excluded))
    return spec
