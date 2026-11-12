"""Bounded P0 confirmation using observed history, never posterior statistics."""
from .stage_aware import StageAware
from ..schema import Action, Decision


class StageAwareP0Confirm(StageAware):
    """Confirm one failed initial candidate while retaining revision budget.

    P0 gets at most three executions in total, including shared H1. Any retry
    must leave two Target executions available for revised candidates. Once a
    revision has run, use the original StageAware behavior, even if its bytes
    equal P0. Common success/error/budget stops are inherited unchanged.
    """
    reject_initial_payload_revisions = True
    MAX_P0_EXECUTIONS = 3
    RESERVED_REVISION_EXECUTIONS = 2

    @staticmethod
    def _initial_candidate_active(history):
        first = history.completed[0]
        # Missing candidate identity cannot establish that this is P0.
        return all(e.payload_id == 'P0' and e.payload_sha256 == first.payload_sha256
                   for e in history.completed)

    def on_failure(self, history):
        if not self._initial_candidate_active(history):
            return super().on_failure(history)
        attempts = history.current_payload_attempts
        if len(attempts) >= self.MAX_P0_EXECUTIONS:
            return Decision(Action.REVISE, 'p0_execution_cap')
        if history.remaining_target_budget <= self.RESERVED_REVISION_EXECUTIONS:
            return Decision(Action.REVISE, 'p0_reserve_revision_budget')
        # U and observer failures are not negative observations. Never merge
        # predicates from separate executions into a fabricated Trigger=T.
        states = [e.trigger for e in attempts
                  if e.stage != 'ERROR' and not e.observation_error]
        positives, negatives = states.count('T'), states.count('F')
        if negatives >= 2 and positives == 0:
            return Decision(Action.REVISE, 'p0_confirmed_trigger_false')
        if positives and negatives:
            return Decision(Action.RETRY, 'p0_mixed_trigger_confirmation')
        if len(attempts) == 1 and negatives == 1:
            return Decision(Action.RETRY, 'p0_confirm_trigger_false')
        return super().on_failure(history)
