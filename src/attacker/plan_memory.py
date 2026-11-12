"""Allowlisted, attributed planning intent; never an observation channel.

Capsules describe the plan that produced a candidate. Observed outcomes are
attached separately by revision_memory after that candidate has executed.
"""
from copy import deepcopy
import re


SCHEMA = 'revision_plan_v1'
STATUS = 'unverified_model_plan'


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + ' must be nonempty text')
    return value


def _identity(value, label):
    value = _text(value, label)
    if '/' in value:
        raise ValueError(label + ' must be one identity component')
    return value


def _hash(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError(label + ' must be a SHA-256 digest')
    return value


def _strings(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(label + ' must be a list of strings')
    return list(value)


def _previous_plan_review(value):
    """Project decisions about earlier hypotheses, not new observed facts."""
    if not isinstance(value, list):
        raise ValueError('Previous plan reviews must be a list')
    projected, seen = [], set()
    for review in value:
        if not isinstance(review, dict):
            raise ValueError('Previous plan review must be an object')
        plan_id = _text(review.get('plan_id'), 'Reviewed plan identity')
        if plan_id in seen:
            raise ValueError('A previous plan may be reviewed only once')
        seen.add(plan_id)
        if (review.get('assessment') not in ('supported', 'contradicted', 'inconclusive')
                or review.get('decision') not in ('retain', 'adjust', 'abandon')):
            raise ValueError('Invalid previous plan assessment or decision')
        references = _strings(review.get('outcome_refs'), 'Reviewed outcome references')
        if not references or any(not ref.strip() for ref in references):
            raise ValueError('Previous plan review requires outcome references')
        preserve = _strings(review.get('preserve'), 'Previous plan preservation')
        if any(not item.strip() for item in preserve):
            raise ValueError('Previous plan preservation must contain nonempty strings')
        projected.append(dict(plan_id=plan_id, assessment=review['assessment'],
                              decision=review['decision'], outcome_refs=references,
                              reason=_text(review.get('reason'), 'Previous plan decision reason'),
                              preserve=preserve))
    return projected


def _summary(value):
    if not isinstance(value, dict):
        raise ValueError('Plan summary must be an object')
    edits = value.get('edits')
    if not isinstance(edits, list) or not 1 <= len(edits) <= 3:
        raise ValueError('Plan summary requires one to three bounded edits')
    projected = []
    for edit in edits:
        if not isinstance(edit, dict) or edit.get('basis') not in ('observation', 'hypothesis'):
            raise ValueError('Invalid plan summary edit')
        references = _strings(edit.get('references'), 'Edit references')
        if not references or any(not ref.strip() for ref in references):
            raise ValueError('Plan summary edits require source references')
        projected.append(dict(change=_text(edit.get('change'), 'Edit change'),
                              basis=edit['basis'], references=references,
                              expected_effect=_text(edit.get('expected_effect'), 'Expected effect')))
    summary = dict(hypothesis=_text(value.get('hypothesis'), 'Plan hypothesis'),
                predicted_observation=_text(value.get('predicted_observation'), 'Plan prediction'),
                preserve=_strings(value.get('preserve'), 'Plan preservation'),
                unknowns=_strings(value.get('unknowns'), 'Plan uncertainty'),
                edits=projected,
                strategy_rationale=_text(value.get('strategy_rationale'), 'Strategy rationale'))
    if 'previous_plan_review' in value:
        summary['previous_plan_review'] = _previous_plan_review(value['previous_plan_review'])
    return summary


def make_plan_memory(plan, *, case_id, policy_id, revision_id,
                     parent_payload_sha256, payload_sha256, source_attempt_count):
    """Summarize a validated final plan without another model call or clipping.

Only the explicitly named model annotations are copied. The controller supplies
all identity and lineage fields, including the number of completed attempts
visible when this plan was made.
"""
    if not isinstance(plan, dict) or not isinstance(plan.get('hypothesis_test'), dict):
        raise ValueError('A structured validated plan is required')
    unknowns = _strings(plan.get('unknowns'), 'Plan uncertainty')
    for item in _strings(plan['hypothesis_test'].get('unknowns', []), 'Hypothesis uncertainty'):
        if item not in unknowns:
            unknowns.append(item)
    capsule = dict(schema=SCHEMA, plan_id=f'{case_id}/{policy_id}/{revision_id}',
                   case_id=case_id, policy_id=policy_id, revision_id=revision_id,
                   parent_payload_sha256=parent_payload_sha256,
                   payload_sha256=payload_sha256, source_attempt_count=source_attempt_count,
                   status=STATUS, summary=dict(
                       hypothesis=plan.get('hypothesis'),
                       predicted_observation=plan['hypothesis_test'].get('predicted_observation'),
                       preserve=plan.get('preserve'), unknowns=unknowns,
                       edits=plan.get('edits'), strategy_rationale=plan.get('strategy_rationale')))
    if 'previous_plan_review' in plan:
        capsule['summary']['previous_plan_review'] = plan['previous_plan_review']
    return project_plan_memory(capsule, case_id=case_id, policy_id=policy_id,
                               payload_sha256=payload_sha256)


def project_plan_memory(capsule, *, case_id, policy_id, payload_sha256):
    """Validate capsule identity and return only the permitted annotation fields.

The caller validates historical base/prefix linkage. A shared capsule may be
carried into a policy branch, just as shared execution history can be; capsules
from another non-shared policy are rejected.
"""
    if not isinstance(capsule, dict) or capsule.get('schema') != SCHEMA:
        raise ValueError('Invalid revision plan capsule')
    case = _identity(capsule.get('case_id'), 'Plan case identity')
    policy = _identity(capsule.get('policy_id'), 'Plan policy identity')
    revision = _identity(capsule.get('revision_id'), 'Plan revision identity')
    plan_id = f'{case}/{policy}/{revision}'
    if capsule.get('plan_id') != plan_id or case != case_id:
        raise ValueError('Revision plan identity differs from its source')
    if policy_id is not None and policy not in ('shared', policy_id):
        raise ValueError('Revision plan belongs to another policy')
    sha256 = _hash(capsule.get('payload_sha256'), 'Plan candidate hash')
    if sha256 != payload_sha256:
        raise ValueError('Revision plan does not match candidate bytes')
    parent = _hash(capsule.get('parent_payload_sha256'), 'Plan base hash')
    count = capsule.get('source_attempt_count')
    if type(count) is not int or count < 1:
        raise ValueError('Revision plan requires a positive source attempt count')
    if capsule.get('status') != STATUS:
        raise ValueError('Revision plans must remain unverified model annotations')
    return deepcopy(dict(schema=SCHEMA, plan_id=plan_id, case_id=case,
                         policy_id=policy, revision_id=revision,
                         parent_payload_sha256=parent, payload_sha256=sha256,
                         source_attempt_count=count, status=STATUS,
                         summary=_summary(capsule.get('summary'))))
