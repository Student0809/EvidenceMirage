from .base import DecisionPolicy
from ..schema import Action, Decision


class FixedSchedule(DecisionPolicy):
    def __init__(self, schedule=('Retry', 'Revise', 'Retry')):
        if not schedule or any(a not in ('Retry', 'Revise') for a in schedule):
            raise ValueError('Schedule must contain Retry/Revise')
        self.schedule = tuple(schedule)

    def on_failure(self, history):
        return Decision(Action(self.schedule[history.spent_target_budget % len(self.schedule)]), 'fixed_schedule')
