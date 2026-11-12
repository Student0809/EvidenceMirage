"""Independent tool-evidence visibility and PIMiner historical-library switches."""
from dataclasses import dataclass, replace

GROUPS = ('G00', 'G10', 'G01', 'G11')
NO_HISTORY = 'Historical PIMiner strategy library disabled for this ablation.'
ABLATION_PROTOCOL = 'tool_evidence_piminer_library_v3'


@dataclass(frozen=True)
class FeedbackAblation:
    tool_evidence: bool = True
    historical_experience: bool = True

    def __post_init__(self):
        if any(type(v) is not bool for v in (self.tool_evidence, self.historical_experience)):
            raise ValueError('A/P switches must be booleans')

    @property
    def group(self):
        return f'G{int(self.tool_evidence)}{int(self.historical_experience)}'

    @property
    def feedback_view(self):
        return 'authorized_evidence' if self.tool_evidence else 'final_only'

    @classmethod
    def resolve(cls, value=None):
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()  # V1 already supplies the selected PIMiner library document.
        if value not in GROUPS:
            raise ValueError('Unknown feedback ablation group')
        return cls(value[1] == '1', value[2] == '1')


class AssessmentModules:
    def __init__(self, ablation=None, judgment_memory=None):
        self.config = FeedbackAblation.resolve(ablation)
        if judgment_memory is not None:
            raise ValueError('--judgment-memory is obsolete: P now controls the existing V1 PIMiner strategy library')

    def metadata(self):
        return dict(protocol=ABLATION_PROTOCOL, group=self.config.group,
                    tool_evidence=self.config.tool_evidence,
                    feedback_view=self.config.feedback_view,
                    structured_assessment=False,
                    historical_experience=self.config.historical_experience,
                    assessment_mode='ordinary',
                    memory_mode='piminer_strategy_document' if self.config.historical_experience else 'disabled',
                    experience_scope='assessment_planning_generation_revision',
                    initial_payload='shared_fixed_p0')

    def project(self, attempt, history):
        # Validate original identity before projecting every history item consistently.
        from .history import validate_history
        validate_history(attempt, history)
        projected = [dict(item, attempt=replace(
            item['attempt'],
            strategy_document=(item['attempt'].strategy_document
                               if self.config.historical_experience else NO_HISTORY),
            packets=item['attempt'].packets if self.config.tool_evidence else ()))
            for item in history]
        return projected[-1]['attempt'], projected

    def assess(self, attempt, history, budget, decide, *, feedback_view):
        from .assessment import assess
        attempt, history = self.project(attempt, history)
        return assess(attempt, history, budget, decide, feedback_view=feedback_view,
                      structured=False)


def add_arguments(parser):
    parser.add_argument('--ablation', choices=GROUPS,
                        help='A=extra tool evidence visible to attacker, P=PIMiner historical strategy library; ordinary assessment in all groups; default G11')
    parser.add_argument('--judgment-memory', help='Obsolete; rejected. P uses the existing V1 library.')
