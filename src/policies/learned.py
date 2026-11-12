"""A fitted one-step value comparison over completed legal evidence."""
import json
from pathlib import Path

from .base import DecisionPolicy
from ..schema import Action, Decision
from ..decision_learning.features import context_from_history
from ..decision_learning.model import predict, select_action, usable_context, validate_model

ROOT = Path(__file__).resolve().parents[2]


class LearnedDecision(DecisionPolicy):
    def __init__(self, model_path, revision_penalty=0.0, tie_margin=0.0,
                 fallback_action='Retry', allow_synthetic=False):
        self.model_path = (ROOT / model_path).resolve()
        self.model = validate_model(json.loads(self.model_path.read_text()))
        if type(allow_synthetic) is not bool:
            raise ValueError('allow_synthetic must be boolean')
        if self.model['kind'] == 'SYNTHETIC_FIXTURE' and not allow_synthetic:
            raise ValueError('Synthetic model requires explicit fixture-only opt-in')
        self.options = dict(revision_penalty=revision_penalty, tie_margin=tie_margin,
                            fallback_action=fallback_action)
        # Validate numerical/action options at load time, before any Target call.
        from ..decision_learning.model import _validate_decision_options
        _validate_decision_options(**self.options)

    def on_failure(self, history):
        context = context_from_history(history)
        probabilities = predict(self.model, context)
        action = select_action(probabilities, context, **self.options)
        delta = probabilities['Retry'] - probabilities['Revise'] + self.options['revision_penalty']
        reason = ('learned_unavailable_fallback' if not usable_context(context)
                  else 'learned_tie_fallback' if abs(delta) <= self.options['tie_margin']
                  else 'learned_one_step_value')
        return Decision(Action(action),
                        f"{reason}; q_retry={probabilities['Retry']:.8f}; "
                        f"q_revise={probabilities['Revise']:.8f}; adjusted_delta={delta:.8f}")


def validate_runtime(config, cases):
    """Preserve synthetic/live, observed-scope and held-out boundaries."""
    options = config.get('policies', {}).get('learned')
    if options is None:
        return
    policy = LearnedDecision(**options)
    if config['backend'] == 'agentdojo' and policy.model['kind'] != 'LIVE_AGENTDOJO':
        raise ValueError('Synthetic decision models cannot run live AgentDojo experiments')
    for field in ('target', 'attacker'):
        if policy.model['protocol'][field] != config[field]:
            raise ValueError('Learned model protocol differs from runtime ' + field)
    from ..evidence.observer import FixedObserver
    configured_queries = set(FixedObserver(**config['observation']).queries)
    trained_queries = set(FixedObserver(**policy.model['protocol']['observation']).queries)
    if configured_queries != trained_queries:
        raise ValueError('Learned model protocol differs from runtime observation')
    from ..evidence.observer import policy_for_case
    from ..vendor.attacker_observer import definitions_for
    for case in cases:
        spec = case['goal_spec']
        selected = [p for p in spec['predicates'] if p['id'] in spec['trigger_ids']]
        if not selected:
            raise ValueError('Learned policy requires a predeclared observable Trigger: ' + case['case_id'])
        available = set(definitions_for(policy_for_case(case))) & configured_queries
        if any(p['query'] not in available for p in selected):
            raise ValueError('Learned Trigger query is not available: ' + case['case_id'])
        if config['split'] == 'test':
            if (case['base_task_id'] in policy.model['training_base_task_ids']
                    or case.get('group_id', case['base_task_id']) in policy.model.get('training_group_ids', ())):
                raise ValueError('Learned policy test task overlaps model training data')
