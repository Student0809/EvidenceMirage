"""Policy input contains only completed episodes, no raw data or future outcomes."""
from dataclasses import dataclass
from ..schema import Episode


@dataclass(frozen=True)
class EvidenceSummary:
    predicate_id: str
    query: str
    status: str
    is_trigger: bool


@dataclass(frozen=True)
class ObservationSummary:
    episode_id: str
    payload_sha256: str
    stage: str
    trigger: str
    response_signal: str
    evidence: tuple[EvidenceSummary, ...] = ()
    observation_error: bool = False
    payload_id: str | None = None


@dataclass(frozen=True)
class History:
    completed: tuple[ObservationSummary, ...]
    remaining_target_budget: int
    spent_target_budget: int

    @classmethod
    def from_episodes(cls, episodes: list[Episode], budget):
        return cls(tuple(ObservationSummary(e.episode_id, e.payload_sha256, e.stage,
                                            e.trigger, e.response_signal,
                                            tuple(EvidenceSummary(p['id'], p['query'], p['status'],
                                                                  p.get('is_trigger', False))
                                                  for p in e.evidence),
                                            e.observation_error is not None, e.payload_id) for e in episodes),
                   budget.remaining, budget.spent)

    @property
    def last(self):
        return self.completed[-1]

    @property
    def current_payload_attempts(self):
        return tuple(e for e in self.completed if e.payload_sha256 == self.last.payload_sha256)

    def summary(self):
        attempts = self.current_payload_attempts
        return dict(payload_sha256=self.last.payload_sha256, attempts=len(attempts),
                    observed_trigger_true=sum(e.trigger == 'T' for e in attempts),
                    observed_trigger_false=sum(e.trigger == 'F' for e in attempts),
                    observed_trigger_unknown=sum(e.trigger == 'U' for e in attempts),
                    official_gold_successes=sum(e.stage == 'SUCCESS' for e in attempts),
                    last_stage=self.last.stage, retry_count=max(0, len(attempts) - 1),
                    remaining_target_budget=self.remaining_target_budget)
