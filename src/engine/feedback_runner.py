"""Autonomous actions over public feedback, shared by static replay and live sources."""
from dataclasses import replace
from ..feedback.history import history_item
from ..feedback.schema import BranchResult, PublicAttempt, VIEWS


class FeedbackRunner:
    def __init__(self, attacker, *, feedback_view='final_only', target_budget=4, source=None,
                 persist=lambda record: None, initial_attempts=0):
        if feedback_view not in VIEWS or type(target_budget) is not int or target_budget < 0:
            raise ValueError('Invalid view or target budget')
        if type(initial_attempts) is not int or initial_attempts < 0:
            raise ValueError('Initial attempt count must be a nonnegative integer')
        self.attacker, self.view, self.limit = attacker, feedback_view, target_budget
        self.source, self.persist = source, persist
        self.initial_attempts = initial_attempts

    def run(self, initial):
        if not isinstance(initial, PublicAttempt):
            raise TypeError('Public initial feedback required')
        result = BranchResult('running')
        result.target_attempts = self.initial_attempts
        modules = getattr(self.attacker, 'assessment_modules', None)
        if modules is not None:
            result.ablation = modules.metadata()
        history = [history_item(initial)]
        current = initial
        while True:
            if current.error:
                result.status = 'target_error'; result.error_type = current.error
                break
            remaining = self.limit - result.target_attempts
            try:
                decision = self.attacker.assess_and_decide(current, history, remaining, feedback_view=self.view)
            except Exception as exc:
                result.status='judgment_error'; result.error_type=type(exc).__name__
                break
            history[-1]['assessment'] = decision
            result.decisions.append(decision.to_dict())
            self.persist(result)
            if decision.action == 'Stop':
                result.status = 'self_reported_complete'
                break
            if remaining == 0:
                result.status = 'budget_exhausted'
                break
            payload = current.payload
            generation = None
            if decision.action == 'Revise':
                try:
                    payload, generation = self.attacker.revise(current, history, decision, feedback_view=self.view)
                    result.generations.append(generation)
                    from ..feedback.candidate import substitution
                    try:
                        generation['slot_validation']=substitution(current,payload)
                    except ValueError as exc:
                        generation.update(status='GENERATION_ERROR',error_type=type(exc).__name__)
                        generation['slot_validation']=dict(status='failed',error_type=type(exc).__name__)
                        raise
                    self.persist(result)
                except Exception as exc:
                    result.status='generation_error'; result.error_type=type(exc).__name__
                    if hasattr(exc, 'record'):
                        result.generations.append(exc.record)
                    break
            if self.source is None:
                result.status = 'revision_ready' if decision.action == 'Revise' else 'retry_requested'
                break
            result.target_attempts += 1  # Reserve before dispatch, including failed attempts.
            self.persist(result)
            try:
                received = self.source(current, payload, result.target_attempts)
                if (not isinstance(received, PublicAttempt) or received.origin != 'target_execution'
                        or received.payload != payload or received.case != current.case
                        or received.strategy_document != current.strategy_document):
                    raise ValueError('Live source returned foreign or nonpublic feedback')
                current = replace(received, analysis=generation.get('analysis', '') if generation else current.analysis,
                                  revision_plan=generation.get('revision_plan') if generation else current.revision_plan)
            except Exception as exc:
                result.status='target_error'; result.error_type=type(exc).__name__
                break
            history.append(history_item(current))
        self.persist(result)
        return result
