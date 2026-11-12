"""Records shared by every adapter and policy. Gold is never inferred from evidence."""
from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
from typing import Any


class Action(str, Enum):
    RETRY = 'Retry'
    REVISE = 'Revise'
    STOP = 'Stop'


class Stage(str, Enum):
    SUCCESS = 'SUCCESS'
    TRIGGERED_FAIL = 'TRIGGERED_FAIL'
    NOT_TRIGGERED = 'NOT_TRIGGERED'
    UNOBSERVABLE = 'UNOBSERVABLE'
    ERROR = 'ERROR'


def payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Payload:
    id: str
    text: str

    def __post_init__(self):
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError('Payload must be nonempty text')

    @property
    def sha256(self):
        return payload_hash(self.text)


@dataclass
class TargetResult:
    gold: bool | None
    final: str = ''
    utility: bool | None = None
    observation: dict | None = None
    usage: dict | None = None
    error: str | None = None
    observation_error: str | None = None
    seconds: float = 0.0
    raw: dict = field(default_factory=dict)
    black_box_final: str | None = None

    def __post_init__(self):
        if self.error is not None:
            if self.gold is not None:
                raise ValueError('Target errors cannot carry a Gold verdict')
        elif type(self.gold) is not bool:
            raise ValueError('A valid Target result requires an official boolean Gold')


@dataclass(frozen=True)
class Episode:
    episode_id: str
    case_id: str
    policy_id: str
    payload_id: str
    payload_sha256: str
    phase: str
    status: str
    gold: bool | None
    final: str
    trigger: str
    stage: str
    response_signal: str
    evidence: tuple
    observation: dict | None
    usage: dict | None
    error: str | None
    observation_error: str | None
    target_seconds: float
    query_units: int
    raw_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
