"""Deterministic, history-balanced logistic estimates for fixed candidate adoption.

Gold appears only in fitting targets and held-out outcomes. This is a one-step
model, not a finite-horizon controller or an estimate of revision generation
reliability. Serialization is plain finite JSON and never executable pickle.
"""
import json
import math
from collections import Counter, defaultdict

from .dataset import ACTIONS, KINDS, complete_pairs, protocol_fingerprint, validate_rows
from .features import FEATURE_NAMES, FEATURE_VERSION, feature_vector, validate_context

MODEL_TYPE = 'paired_logistic_v1'
LIMITATIONS = [
    'Labels estimate adoption of a fixed revision generated before paired scoring; they do not measure online revision-generation reliability.',
    'Complete-case estimates exclude missing/error pairs and may have selection bias.',
    'Predictions are one-step success estimates; tie margins are not calibrated confidence intervals.',
]


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _sigmoid(value):
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1.0 + exp)


def _dot(weights, values):
    return sum(w * x for w, x in zip(weights, values))


def _loss(weights, vectors, targets, l2):
    total = 0.0
    for values, target in zip(vectors, targets):
        score = _dot(weights, values)
        # softplus(score) - target*score, with no exp overflow.
        total += max(score, 0.0) - target * score + math.log1p(math.exp(-abs(score)))
    return total / len(vectors) + 0.5 * l2 * sum(w * w for w in weights[1:])


def _fit_head(vectors, targets, l2, steps, learning_rate):
    weights = [0.0] * len(FEATURE_NAMES)
    initial_loss = current_loss = _loss(weights, vectors, targets, l2)
    completed = 0
    reason = 'max_steps'
    for iteration in range(steps):
        gradient = [0.0] * len(weights)
        for values, target in zip(vectors, targets):
            error = _sigmoid(_dot(weights, values)) - target
            for i, x in enumerate(values):
                gradient[i] += error * x / len(vectors)
        for i in range(1, len(weights)):
            gradient[i] += l2 * weights[i]
        rate = learning_rate
        accepted = False
        for _ in range(50):
            candidate = [w - rate * g for w, g in zip(weights, gradient)]
            if all(math.isfinite(w) for w in candidate):
                candidate_loss = _loss(candidate, vectors, targets, l2)
                if math.isfinite(candidate_loss) and candidate_loss <= current_loss:
                    accepted = True
                    break
            rate *= 0.5
        if not accepted:
            reason = 'line_search_stopped'
            break
        improvement = current_loss - candidate_loss
        weights, current_loss = candidate, candidate_loss
        completed = iteration + 1
        if improvement < 1e-12:
            reason = 'small_loss_change'
            break
    return weights, dict(initial_loss=initial_loss, final_loss=current_loss,
                         steps_completed=completed, stop_reason=reason,
                         target_mean=sum(targets) / len(targets),
                         single_class=all(y == 0 for y in targets) or all(y == 1 for y in targets))


def fit_model(rows, *, l2=0.01, steps=800, learning_rate=0.1):
    if not _finite_number(l2) or l2 < 0:
        raise ValueError('l2 must be finite and nonnegative')
    if type(steps) is not int or steps < 1:
        raise ValueError('steps must be a positive integer')
    if not _finite_number(learning_rate) or learning_rate <= 0:
        raise ValueError('learning_rate must be finite and positive')
    rows = validate_rows(rows)
    train, accounting = complete_pairs(rows, 'train')
    if not train:
        raise ValueError('No complete training pairs; no labels will be invented')
    # Sort gives byte-stable fitting independent of input file ordering.
    train = sorted(train, key=lambda r: r['pair_id'])
    vectors = [feature_vector(r['context']) for r in train]
    targets = {a: [sum(r['outcomes'][a]) / len(r['outcomes'][a]) for r in train] for a in ACTIONS}
    heads, diagnostics = {}, {}
    for action in ACTIONS:
        weights, diagnostic = _fit_head(vectors, targets[action], l2, steps, learning_rate)
        heads[action], diagnostics[action] = {'weights': weights}, diagnostic
    warnings = ['single_class:' + a for a in ACTIONS if diagnostics[a]['single_class']]
    equal_outcome_histories = sum(x == y for x, y in zip(targets['Retry'], targets['Revise']))
    if equal_outcome_histories == len(train):
        warnings.append('all_histories_have_equal_arm_outcome_means')
    if len({tuple(v) for v in vectors}) == 1:
        warnings.append('all_training_contexts_have_identical_features')
    model = dict(
        schema_version=1, model_type=MODEL_TYPE, feature_version=FEATURE_VERSION,
        feature_names=list(FEATURE_NAMES), kind=train[0]['kind'],
        protocol=json.loads(protocol_fingerprint(train[0]['protocol'])),
        training_base_task_ids=sorted({r['base_task_id'] for r in rows if r['split'] == 'train'}),
        training_group_ids=sorted({r.get('group_id', r['base_task_id']) for r in rows if r['split'] == 'train'}),
        training_pair_ids=[r['pair_id'] for r in train], heads=heads,
        training_summary=dict(accounting=accounting, heads=diagnostics, warnings=warnings,
                              equal_outcome_histories=equal_outcome_histories,
                              objective='mean per-history binomial cross-entropy plus L2 (bias unpenalized)',
                              l2=l2, requested_steps=steps, learning_rate=learning_rate,
                              deterministic=True, calibrated=False, limitations=list(LIMITATIONS)),
    )
    validate_model(model)
    return model


def validate_model(model):
    if not isinstance(model, dict):
        raise ValueError('Model must be a JSON object')
    if (type(model.get('schema_version')) is not int or model['schema_version'] != 1
            or model.get('model_type') != MODEL_TYPE
            or type(model.get('feature_version')) is not int or model['feature_version'] != FEATURE_VERSION
            or model.get('feature_names') != list(FEATURE_NAMES)):
        raise ValueError('Incompatible model schema or feature layout')
    if model.get('kind') not in KINDS:
        raise ValueError('Unknown model provenance')
    protocol_fingerprint(model.get('protocol'))
    for field in ('training_base_task_ids', 'training_group_ids', 'training_pair_ids'):
        ids = model.get(field)
        if (not isinstance(ids, list) or not ids or any(not isinstance(v, str) or not v for v in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('Invalid model ' + field)
    if not isinstance(model.get('training_summary'), dict):
        raise ValueError('Missing fit diagnostics')
    heads = model.get('heads')
    if not isinstance(heads, dict) or set(heads) != set(ACTIONS):
        raise ValueError('Missing action heads')
    for action in ACTIONS:
        head = heads[action]
        if not isinstance(head, dict) or set(head) != {'weights'}:
            raise ValueError('Invalid head')
        weights = head['weights']
        if (not isinstance(weights, list) or len(weights) != len(FEATURE_NAMES)
                or not all(_finite_number(w) for w in weights)):
            raise ValueError('Weights must match features and be finite')
    try:
        json.dumps(model, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError('Model must be finite JSON') from error
    return model


def predict(model, context):
    validate_model(model)
    vector = feature_vector(context)
    scores = {a: _dot(model['heads'][a]['weights'], vector) for a in ACTIONS}
    if not all(math.isfinite(s) for s in scores.values()):
        raise ValueError('Nonfinite prediction; model weights are unusable')
    return {a: _sigmoid(scores[a]) for a in ACTIONS}


def usable_context(context):
    validate_context(context)
    return (not context['observation_error']
            and any(p['is_trigger'] and p['status'] in ('T', 'F') for p in context['predicates']))


def _validate_decision_options(revision_penalty, tie_margin, fallback_action):
    if not _finite_number(revision_penalty) or revision_penalty < 0:
        raise ValueError('Revision penalty must be finite and nonnegative')
    if not _finite_number(tie_margin) or tie_margin < 0:
        raise ValueError('Tie margin must be finite and nonnegative')
    if fallback_action not in ACTIONS:
        raise ValueError('Unknown fallback action')


def select_action(probabilities, context, *, revision_penalty=0.0, tie_margin=0.0, fallback_action='Retry'):
    _validate_decision_options(revision_penalty, tie_margin, fallback_action)
    if (not isinstance(probabilities, dict) or set(probabilities) != set(ACTIONS)
            or any(not _finite_number(p) or not 0 <= p <= 1 for p in probabilities.values())):
        raise ValueError('Expected two finite probabilities')
    difference = probabilities['Retry'] - (probabilities['Revise'] - revision_penalty)
    if not usable_context(context) or abs(difference) <= tie_margin:
        return fallback_action
    return 'Retry' if difference > 0 else 'Revise'


def _mean(values):
    return sum(values) / len(values) if values else None


def evaluate_model(model, rows, *, split='dev', revision_penalty=0.0, tie_margin=0.0, fallback_action='Retry'):
    validate_model(model)
    _validate_decision_options(revision_penalty, tie_margin, fallback_action)
    if split not in ('dev', 'test'):
        raise ValueError('Held-out evaluation requires dev or test split')
    rows = validate_rows(rows)
    selected_rows = [r for r in rows if r['split'] == split]
    if any(r['base_task_id'] in set(model['training_base_task_ids']) for r in selected_rows):
        raise ValueError('Held-out base task overlaps model training groups')
    if any(r.get('group_id', r['base_task_id']) in set(model['training_group_ids']) for r in selected_rows):
        raise ValueError('Held-out normalized-prompt group overlaps model training groups')
    if any(r['pair_id'] in set(model['training_pair_ids']) for r in selected_rows):
        raise ValueError('Held-out pair overlaps model training data')
    expected_protocol = protocol_fingerprint(model['protocol'])
    if any(protocol_fingerprint(r['protocol']) != expected_protocol or r['kind'] != model['kind']
           for r in selected_rows):
        raise ValueError('Evaluation provenance/protocol differs from model')
    complete, accounting = complete_pairs(rows, split)
    by_task, by_group = defaultdict(list), defaultdict(list)
    details, brier, outcomes, actions = [], {a: [] for a in ACTIONS}, {a: [] for a in ACTIONS}, Counter()
    selected_outcomes = []
    for row in complete:
        probabilities = predict(model, row['context'])
        action = select_action(probabilities, row['context'], revision_penalty=revision_penalty,
                               tie_margin=tie_margin, fallback_action=fallback_action)
        means = {a: _mean(row['outcomes'][a]) for a in ACTIONS}
        for a in ACTIONS:
            outcomes[a].append(means[a])
            brier[a].append(_mean([(probabilities[a] - float(y)) ** 2 for y in row['outcomes'][a]]))
        selected_outcomes.append(means[action])
        actions[action] += 1
        observed = dict(selected=means[action], **means)
        by_task[row['base_task_id']].append(observed)
        by_group[row.get('group_id', row['base_task_id'])].append(observed)
        details.append(dict(pair_id=row['pair_id'], base_task_id=row['base_task_id'],
                            action=action, probabilities=probabilities, observed_arm_means=means))
    task_means = [{k: _mean([r[k] for r in group]) for k in ('selected',) + ACTIONS}
                  for group in by_task.values()]
    group_means = [{k: _mean([r[k] for r in group]) for k in ('selected',) + ACTIONS}
                   for group in by_group.values()]
    differences = {}
    for action in ACTIONS:
        task_diffs = [r['selected'] - r[action] for r in task_means]
        average = _mean(task_diffs)
        se = (math.sqrt(sum((d - average) ** 2 for d in task_diffs) / (len(task_diffs) - 1) / len(task_diffs))
              if len(task_diffs) > 1 else None)
        group_diffs = [r['selected'] - r[action] for r in group_means]
        group_average = _mean(group_diffs)
        group_se = (math.sqrt(sum((d - group_average) ** 2 for d in group_diffs)
                              / (len(group_diffs) - 1) / len(group_diffs))
                    if len(group_diffs) > 1 else None)
        differences['vs_always_' + action.lower()] = dict(
            history_mean=_mean([s - b for s, b in zip(selected_outcomes, outcomes[action])]),
            task_macro_mean=average, task_standard_error=se,
            group_macro_mean=group_average, group_standard_error=group_se)
    return dict(
        schema_version=1, kind=model['kind'], split=split, accounting=accounting,
        per_arm_brier={a: _mean(brier[a]) for a in ACTIONS},
        selected_mean_outcome=_mean(selected_outcomes),
        always_retry_mean_outcome=_mean(outcomes['Retry']),
        always_revise_mean_outcome=_mean(outcomes['Revise']),
        task_macro={k: _mean([r[k] for r in task_means]) for k in ('selected',) + ACTIONS},
        group_macro={k: _mean([r[k] for r in group_means]) for k in ('selected',) + ACTIONS},
        calibrated=False,
        paired_differences=differences, action_counts=dict(actions), rows=details,
        revision_penalty=revision_penalty, tie_margin=tie_margin, fallback_action=fallback_action,
        limitations=list(LIMITATIONS) + ['Empirical selected-arm outcomes are not oracle regret or a live policy efficacy claim.'],
    )
