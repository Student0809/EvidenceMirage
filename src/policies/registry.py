from .always_retry import AlwaysRetry
from .always_revise import AlwaysRevise
from .fixed_schedule import FixedSchedule
from .label_based import LabelBased
from .stage_aware import StageAware
from .stage_aware_p0_confirm import StageAwareP0Confirm
from .stage_aware_no_evidence import StageAwareNoEvidence
from .learned import LearnedDecision


REGISTRY = dict(always_retry=AlwaysRetry, always_revise=AlwaysRevise,
                fixed_schedule=FixedSchedule, label_based=LabelBased, stage_aware=StageAware, stage_aware_p0_confirm=StageAwareP0Confirm, stage_aware_no_evidence=StageAwareNoEvidence, learned=LearnedDecision)


def build_policies(config):
    if not config or set(config) - set(REGISTRY):
        raise ValueError('Unknown or empty policy set')
    return {name: REGISTRY[name](**(params or {})) for name, params in config.items()}
