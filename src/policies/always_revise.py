from .base import DecisionPolicy
from ..schema import Action, Decision


class AlwaysRevise(DecisionPolicy):
    def on_failure(self, history):
        return Decision(Action.REVISE, 'revise_every_valid_failure')
