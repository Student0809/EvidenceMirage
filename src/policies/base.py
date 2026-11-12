from abc import ABC, abstractmethod
from ..schema import Action, Decision


class DecisionPolicy(ABC):
    def decide(self, history):
        if history.last.stage == 'SUCCESS':
            return Decision(Action.STOP, 'official_gold_true')
        if history.last.stage == 'ERROR':
            return Decision(Action.STOP, 'target_error_not_gold_false')
        if history.remaining_target_budget == 0:
            return Decision(Action.STOP, 'target_budget_exhausted')
        return self.on_failure(history)

    @abstractmethod
    def on_failure(self, history):
        raise NotImplementedError
