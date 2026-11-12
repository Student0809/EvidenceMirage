"""Project completed branch feedback into an attributed revision memory.

Only ``revision_observation`` is an observation channel. It is produced by the
Runner from its authorized ledger; arbitrary history metadata is never read as
evidence. An archive records witnesses, not an estimate of the best payload.
"""
from copy import deepcopy

from ..schema import payload_hash
from .plan_memory import project_plan_memory


PROTOCOL = 'grounded_history_v1'
OBSERVED_SCOPE = 'authorized_observed_events'
PUBLIC_OBSERVATION_SCOPES = ('returned_call_fields_only', 'authorized_receiver_delivery')
_STATES = ('T', 'F', 'U')


def _text(value, label, *, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValueError(label + ' must be text')
    return value


def _project_observation(observation, case_id, sha256, expected_policy, *, autonomous=False):
    if not isinstance(observation, dict):
        raise ValueError('Revision observation must be an object')
    episode_id = _text(observation.get('episode_id'), 'Episode identity', nonempty=True)
    policy_id = _text(observation.get('policy_id'), 'Policy identity', nonempty=True)
    if observation.get('case_id') != case_id:
        raise ValueError('Revision observation belongs to another case')
    if observation.get('payload_sha256') != sha256:
        raise ValueError('Revision observation does not match payload bytes')
    if expected_policy is not None and policy_id not in ('shared', expected_policy):
        raise ValueError('Revision observation belongs to another policy')
    trigger = observation.get('trigger')
    if trigger not in _STATES:
        raise ValueError('Unknown observed Trigger')
    error = observation.get('observation_error')
    if type(error) is not bool:
        raise ValueError('Observation error must be an explicit boolean')
    predicates = observation.get('predicates')
    if not isinstance(predicates, (list, tuple)):
        raise ValueError('Observed predicates must be a sequence')
    projected, ids = [], set()
    for predicate in predicates:
        if not isinstance(predicate, dict):
            raise ValueError('Observed predicate must be an object')
        identifier = _text(predicate.get('id'), 'Predicate identity', nonempty=True)
        if identifier in ids:
            raise ValueError('Duplicate predicate in an episode')
        ids.add(identifier)
        query = _text(predicate.get('query'), 'Predicate query', nonempty=True)
        status = predicate.get('status')
        if status not in _STATES or type(predicate.get('is_trigger')) is not bool:
            raise ValueError('Invalid observed predicate state')
        projected.append(dict(id=identifier, query=query,
                              status='U' if error else status,
                              is_trigger=predicate['is_trigger']))
    selected = [p['status'] for p in projected if p['is_trigger']]
    aggregate = ('F' if 'F' in selected else
                 'T' if selected and all(s == 'T' for s in selected) else 'U')
    if not error and aggregate != trigger:
        raise ValueError('Trigger is inconsistent with its episode predicates')
    queries = observation.get('query_results', [])
    if not isinstance(queries, (list, tuple)):
        raise ValueError('Observed query results must be a sequence')
    query_results, query_ids = [], set()
    for result in queries:
        if not isinstance(result, dict):
            raise ValueError('Observed query result must be an object')
        query_id = _text(result.get('query_id'), 'Query identity', nonempty=True)
        if query_id in query_ids:
            raise ValueError('Duplicate returned query in an episode')
        query_ids.add(query_id)
        response = result.get('response')
        if not isinstance(response, dict) or 'supported' not in response:
            raise ValueError('Missing returned observation response')
        supported = response['supported']
        if supported is not None and type(supported) is not bool:
            raise ValueError('Invalid returned observation support')
        evidence = _text(response.get('evidence'), 'Returned observation evidence')
        # A collection error invalidates the whole episode's observations,
        # including any results collected before the error occurred.
        if not error:
            item=dict(query_id=query_id,response=dict(supported=supported,evidence=evidence))
            if autonomous:
                if result.get('scope') not in PUBLIC_OBSERVATION_SCOPES or not isinstance(result.get('description'),str):
                    raise ValueError('Public observation requires receipt scope and description')
                item.update(scope=result['scope'],description=result['description'])
            query_results.append(item)
    projected=dict(episode_id=episode_id, case_id=case_id, policy_id=policy_id,
                payload_sha256=sha256, trigger='U' if error else trigger,
                observation_error=error, predicates=projected,
                query_results=query_results)
    if autonomous:
        if observation.get('origin') not in ('controlled_replay','target_execution'):
            raise ValueError('Public observation origin required')
        projected['origin']=observation['origin']
    return projected


def build_revision_memory(case, history, *, feedback_protocol='legacy_judge'):
    """Return a safe current-branch archive without ranking or rebasing.

    Legacy histories are consecutive completed Gold failures. The autonomous
    protocol instead requires valid public feedback with unknown evaluator
    outcomes and stores attacker assessments as unverified annotations.
    Legacy callers may omit
    observations; their Trigger is unknown and no episode identity is invented.
    ``case['policy_id']`` may pin the active branch; otherwise all non-shared
    observations must agree on one policy.
    """
    from ..feedback.schema import PROTOCOL as AUTONOMOUS_PROTOCOL
    if feedback_protocol not in ('legacy_judge', AUTONOMOUS_PROTOCOL):
        raise ValueError('Unknown feedback protocol')
    autonomous = feedback_protocol == AUTONOMOUS_PROTOCOL
    if not isinstance(case, dict):
        raise ValueError('Revision case must be an object')
    case_id = _text(case.get('case_id'), 'Case identity', nonempty=True)
    goal = _text(case.get('injection_task_goal'), 'Public goal')
    user_task = _text(case.get('user_task_prompt'), 'Public user task')
    expected_policy = case.get('policy_id')
    if expected_policy is not None:
        _text(expected_policy, 'Active policy identity', nonempty=True)
    if not isinstance(history, (list, tuple)) or not history:
        raise ValueError('Revision memory requires a nonempty completed history')
    facts = [dict(id='goal', kind='public_input', value=goal, scope='public_input'),
             dict(id='user_task', kind='public_input', value=user_task, scope='public_input')]
    attempts, candidates, plan_outcomes = [], {}, {}
    episode_ids, branch_policies = set(), set()
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            raise ValueError('Completed attempt must be an object')
        if type(item.get('iter')) is not int or item['iter'] != index:
            raise ValueError('Revision history must contain consecutive iterations')
        if autonomous and (item.get('valid') is not True or item.get('feedback_protocol') != AUTONOMOUS_PROTOCOL):
            raise ValueError('Autonomous history requires valid public completed feedback')
        if not autonomous and item.get('security') is not False:
            raise ValueError('Revision history requires valid completed Gold failures')
        injection = _text(item.get('injection'), 'Attempt payload', nonempty=True)
        analysis = _text(item.get('analysis'), 'Attempt model analysis')
        final = _text(item.get('final_output'), 'Attempt model report')
        sha256 = payload_hash(injection)
        observation = None
        if item.get('revision_observation') is not None:
            observation = _project_observation(item['revision_observation'], case_id,
                                               sha256, expected_policy, autonomous=autonomous)
            eid = observation['episode_id']
            if eid in episode_ids:
                raise ValueError('Duplicate revision episode')
            episode_ids.add(eid)
            if observation['policy_id'] != 'shared':
                branch_policies.add(observation['policy_id'])
                if len(branch_policies) > 1:
                    raise ValueError('Revision history mixes policy branches')
        attempt = dict(iter=index, analysis=analysis,
                       analysis_status='unverified_model_analysis',
                       injection=injection, security=False, utility=None,
                       final_output=final, payload_sha256=sha256,
                       trigger=observation['trigger'] if observation else 'U',
                       revision_observation=observation)
        if autonomous:
            attempt.pop('security')
            attempt.pop('utility')
            if item.get('origin') not in ('controlled_replay','target_execution'):
                raise ValueError('Public attempt origin required')
            attempt.update(feedback_protocol=AUTONOMOUS_PROTOCOL, valid=True,origin=item['origin'])
            if observation is not None and observation['origin']!=attempt['origin']:
                raise ValueError('Observation origin differs from attributed feedback')
            assessment = item.get('attacker_assessment')
            if assessment is not None:
                from ..feedback.schema import AssessmentDecision
                if set(assessment) != set(AssessmentDecision.__dataclass_fields__):
                    raise ValueError('Unexpected assessment fields in public history')
                attempt['attacker_assessment'] = deepcopy(assessment)
        revision_plan = None
        if item.get('revision_plan') is not None:
            observed_policy = observation['policy_id'] if observation is not None else None
            revision_plan = project_plan_memory(item['revision_plan'], case_id=case_id,
                policy_id=expected_policy, payload_sha256=sha256)
            if (observed_policy is not None
                    and revision_plan['policy_id'] not in ('shared', observed_policy)):
                raise ValueError('Revision plan belongs to another observed policy')
            source_count = revision_plan['source_attempt_count']
            if (source_count > index
                    or revision_plan['parent_payload_sha256'] != attempts[source_count - 1]['payload_sha256']):
                raise ValueError('Revision plan base differs from its completed history prefix')
            if revision_plan['policy_id'] != 'shared':
                branch_policies.add(revision_plan['policy_id'])
                if len(branch_policies) > 1:
                    raise ValueError('Revision plans mix policy branches')
            plan_id = revision_plan['plan_id']
            existing = plan_outcomes.get(plan_id)
            if existing is not None and existing['revision_plan'] != revision_plan:
                raise ValueError('Conflicting revision plans share an identity')
            outcome = plan_outcomes.setdefault(plan_id, dict(plan_id=plan_id,
                revision_plan=revision_plan, attempt_iters=[], outcome_refs=[]))
            outcome['attempt_iters'].append(index)
            attempt['revision_plan'] = revision_plan
        attempts.append(attempt)
        candidate = candidates.setdefault(sha256, dict(payload_sha256=sha256,
            text=injection, attempt_iters=[], trigger_counts={s: 0 for s in _STATES},
            full_trigger_episode_ids=[]))
        candidate['attempt_iters'].append(index)
        candidate['trigger_counts'][attempt['trigger']] += 1
        fact_metadata = dict(payload_sha256=sha256)
        if autonomous:
            fact_metadata['feedback_origin']=attempt['origin']
        if observation is not None:
            fact_metadata['episode_id'] = observation['episode_id']
        facts.append(dict(id=f'attempt:{index}:payload', kind='payload_text',
                          value=injection, scope='payload_text', **fact_metadata))
        facts.append(dict(id=f'attempt:{index}:final_report', kind='model_report',
                          value=final, scope='model_report_only', **fact_metadata))
        if autonomous and attempt.get('attacker_assessment') is not None:
            facts.append(dict(id=f'attempt:{index}:assessment', kind='attacker_assessment',
                              value=deepcopy(attempt['attacker_assessment']),
                              scope='unverified_model_judgment', **fact_metadata))
        if revision_plan is not None:
            outcome['outcome_refs'].extend([f'attempt:{index}:payload', f'attempt:{index}:final_report'])
        if observation is None:
            continue
        facts.append(dict(id=f'attempt:{index}:trigger', kind='observed_trigger',
                          value=observation['trigger'], scope=OBSERVED_SCOPE,
                          **fact_metadata))
        if revision_plan is not None:
            outcome['outcome_refs'].append(f'attempt:{index}:trigger')
        for predicate in observation['predicates']:
            facts.append(dict(id=f"attempt:{index}:predicate:{predicate['id']}",
                              kind='observed_predicate', value=predicate['status'],
                              predicate_id=predicate['id'], scope=OBSERVED_SCOPE,
                              **fact_metadata))
            if revision_plan is not None:
                outcome['outcome_refs'].append(f"attempt:{index}:predicate:{predicate['id']}")
        if observation['trigger'] == 'T' and not observation['observation_error']:
            candidate['full_trigger_episode_ids'].append(observation['episode_id'])
    # Dict insertion order preserves first appearance of each complete payload.
    positive = [sha for sha, candidate in candidates.items()
                if candidate['full_trigger_episode_ids']]
    result=dict(protocol=PROTOCOL, attempts=attempts,
                         candidates=list(candidates.values()),
                         plan_outcomes=list(plan_outcomes.values()),
                         positive_reference_candidates=positive, facts=facts,
                         base_payload_sha256=attempts[-1]['payload_sha256'])
    if autonomous:
        result['feedback_protocol']=AUTONOMOUS_PROTOCOL
        result['feedback_note']='Controlled_replay receipts describe constructed feedback, not execution caused by the supplied payload. Target_execution receipts concern only their attributed attempt and scope. No hidden outcome is supplied.'
    return deepcopy(result)
