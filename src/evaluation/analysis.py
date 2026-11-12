from collections import Counter


def diagnostics(episodes, decisions):
    previous = {}
    transitions, after_revision = [], []
    for ep in episodes:
        key = ep.case_id, ep.policy_id
        if ep.policy_id == 'shared':
            previous[(ep.case_id, 'shared')] = ep
            continue
        before = previous.get(key, previous.get((ep.case_id, 'shared')))
        if before and before.trigger == 'T' and ep.gold is not None:
            item = dict(before=before.episode_id, after=ep.episode_id,
                        trigger_retained=True if ep.trigger == 'T' else False if ep.trigger == 'F' else None,
                        revised=before.payload_sha256 != ep.payload_sha256)
            transitions.append(item)
            if item['revised']:
                after_revision.append(item)
        previous[key] = ep
    known = [t for t in after_revision if t['trigger_retained'] is not None]
    return dict(stages=dict(Counter(e.stage for e in episodes)),
                trigger_distribution=dict(Counter(e.trigger for e in episodes)),
                response_signals=dict(Counter(e.response_signal for e in episodes)),
                actions=dict(Counter(d['action'] for d in decisions)),
                observation_errors=sum(e.observation_error is not None for e in episodes),
                revision_trigger_preservation=dict(eligible=len(after_revision), known=len(known),
                    unknown=len(after_revision)-len(known),
                    rate=sum(t['trigger_retained'] for t in known)/len(known) if known else None),
                trigger_transitions=transitions)
