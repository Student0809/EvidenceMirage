from .base import DecisionPolicy
from ..schema import Action, Decision


class AlwaysRetry(DecisionPolicy):
    def on_failure(self, history):
        return Decision(Action.RETRY, 'retain_p0')
