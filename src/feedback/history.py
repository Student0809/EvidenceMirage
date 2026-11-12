"""Only controller-owned public attempts and attributed assessments enter history."""
from copy import deepcopy
from .schema import PublicAttempt, PROTOCOL, public_packets


def history_item(attempt, assessment=None):
    if not isinstance(attempt, PublicAttempt):
        raise TypeError('PublicAttempt required')
    return dict(attempt=attempt, assessment=assessment)


def project_history(history, feedback_view):
    visible = []
    for i, item in enumerate(history):
        attempt = item['attempt']
        if attempt.error:
            raise ValueError('Completed history contains invalid feedback')
        row = attempt.view(feedback_view)
        row = {k: row[k] for k in ('injection','analysis','final_output','packets','origin','episode_id')}
        row.update(iter=i, feedback_protocol=PROTOCOL, valid=True)
        row['episode_id'] = attempt.episode_id or f'attempt_{i}'
        row['episode_relation'] = 'current' if i == len(history) - 1 else 'historical'
        if item.get('assessment') is not None:
            row['attacker_assessment'] = item['assessment'].to_dict()
        if attempt.revision_plan is not None:
            row['revision_plan'] = deepcopy(attempt.revision_plan)
        visible.append(row)
    return visible


def validate_history(attempt,history):
    if not history or history[-1]['attempt'] != attempt:
        raise ValueError('History must end at the exact current public feedback')
    episode_ids = set()
    for index,item in enumerate(history):
        previous=item['attempt']
        if (not isinstance(previous,PublicAttempt) or previous.error
                or previous.case!=attempt.case or previous.strategy_document!=attempt.strategy_document):
            raise ValueError('History contains another case, strategy or invalid feedback')
        if previous.episode_id is not None:
            if previous.episode_id in episode_ids:
                raise ValueError('History repeats a Target Episode')
            episode_ids.add(previous.episode_id)
        if index and previous.origin!='target_execution':
            raise ValueError('Static variants cannot be concatenated into a live history')


def revision_history(attempt, history, decision, feedback_view):
    if decision.action != 'Revise' or attempt.error:
        raise ValueError('Revision requires valid feedback and an explicit Revise decision')
    validate_history(attempt,history)
    visible = project_history(history, feedback_view)
    if not visible or visible[-1]['injection'] != attempt.payload.text or visible[-1]['final_output'] != attempt.final_output:
        raise ValueError('Revision history must end with the current feedback')
    for i, item in enumerate(visible):
        packets = public_packets(item.pop('packets'))
        item['revision_observation'] = dict(episode_id=item['episode_id'], case_id='current_case',
            policy_id='autonomous', payload_sha256=history[i]['attempt'].payload.sha256,
            trigger='U', observation_error=False,
            origin=item['origin'],
            predicates=[dict(id=p['query_id'], query=p['query_id'],
                             status={True:'T', False:'F', None:'U'}[p['response']['supported']],
                             is_trigger=False) for p in packets],
            query_results=[dict(query_id=p['query_id'], response=p['response'],
                                description=p['description'],scope=p['scope']) for p in packets])
    visible[-1]['attacker_assessment'] = decision.to_dict()
    return visible
