"""SYNTHETIC_SMOKE stand-in; never an empirical judgment or attack result."""
import json


def decide(prompt,stage):
    if stage=='assessment':
        data = json.loads(prompt.split('PUBLIC INPUT:\n', 1)[1])
        if data.get('assessment_mode') == 'ordinary':
            return json.dumps(dict(goal_status='unknown', action='Revise',
                                   action_reason='SYNTHETIC_SMOKE; exercise pipeline only'))
        return json.dumps(dict(goal_status='unknown',progress='unknown',claim_support='undetermined',disclosure='unknown',
            support_basis='insufficient',basis_refs=[],unknowns=['SYNTHETIC_SMOKE, no inference performed'],
            action='Revise',action_reason='Exercise pipeline only'))
    if stage=='attacker_revise':
        return '<analysis>SYNTHETIC_SMOKE only.</analysis><injection>FIXTURE synthetic candidate, not a generated attack.</injection>'
    if stage in ('planner','plan_revision'):
        data=json.loads(prompt.split('PUBLIC INPUT:\n',1)[1]);context=data['context']
        return json.dumps(dict(action='make_plan',observations=[],cause='unknown',
            hypothesis='SYNTHETIC_SMOKE only',edit_scope='Exercise interface',preserve=[],unknowns=['No inference'],
            objective=context['injection_task_goal'],
            hypothesis_test=dict(support_refs=[],counter_refs=[],unknowns=['No inference'],predicted_observation='Unknown'),
            strategy_rationale='Synthetic interface exercise',edits=[dict(change='Synthetic exercise',
                basis='hypothesis',references=['goal'],expected_effect='No empirical claim')],
            previous_plan_review=[dict(plan_id=o['plan_id'], assessment='inconclusive',
                decision='adjust', outcome_refs=o['outcome_refs'][:1],
                reason='Synthetic interface exercise', preserve=[])
                for o in context['revision_memory'].get('plan_outcomes', [])]))
    if stage in ('generator','static_repair'):
        return '<injection>FIXTURE synthetic candidate, not a generated attack.</injection>'
    if stage=='semantic_review':
        return json.dumps(dict(assessments={k:'unknown' for k in ('plan_goal_alignment',
            'candidate_goal_alignment','plan_following','evidence_alignment')},findings=[]))
    raise ValueError('Unsupported fixture stage')
