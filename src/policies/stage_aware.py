from .base import DecisionPolicy
from .fixed_schedule import FixedSchedule
from ..schema import Action, Decision


class StageAware(DecisionPolicy):
    def __init__(self, max_retries_per_payload=2, unknown_schedule=('Retry', 'Revise', 'Retry'),
                 unknown_action='schedule'):
        if type(max_retries_per_payload) is not int or max_retries_per_payload < 0:
            raise ValueError('Invalid retry cap')
        if unknown_action not in ('schedule', 'revise'):
            raise ValueError('unknown_action must be schedule or revise')
        self.max_retries = max_retries_per_payload
        self.unknown_action = unknown_action
        self.fallback = FixedSchedule(unknown_schedule)

    def on_failure(self, history):
        if history.last.trigger == 'F':
            return Decision(Action.REVISE, 'trigger_false_after_retry' if len(history.current_payload_attempts) > 1
                            else 'trigger_false')
        if history.last.trigger == 'U' and self.unknown_action == 'revise':
            return Decision(Action.REVISE, 'unknown_piminer_revise')
        if len(history.current_payload_attempts) - 1 >= self.max_retries:
            return Decision(Action.REVISE, 'payload_retry_cap')
        if history.last.trigger == 'T':
            return Decision(Action.RETRY, 'preserve_observed_trigger')
        fallback = self.fallback.on_failure(history)
        return Decision(fallback.action, 'unknown_' + fallback.reason)
