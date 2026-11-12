from .base import DecisionPolicy
from ..schema import Action, Decision


class LabelBased(DecisionPolicy):
    def on_failure(self, history):
        action = Action.RETRY if history.last.response_signal == 'Partial' else Action.REVISE
        return Decision(action, 'final_label_' + history.last.response_signal)
