"""No evaluator verdict is part of a valid completed public attempt."""
from copy import deepcopy
from dataclasses import dataclass, field
import json
from ..schema import Payload
from ..evidence.probe_sampling import DELIVERY_SCOPE, validate_public_packet

PROTOCOL = 'autonomous_mismatch_v1'
PUBLIC_CASE_FIELDS = ('suite_name', 'user_task_id', 'injection_task_id',
                      'user_task_prompt', 'injection_task_goal',
                      'context_with_placeholder', 'injection_candidates')
VIEWS = ('final_only', 'authorized_evidence', 'receiver_report_only')
PACKET_SCOPES = {'authorized_evidence': 'returned_call_fields_only',
                 'receiver_report_only': DELIVERY_SCOPE}


def public_case(value):
    result = {k: deepcopy(value[k]) for k in PUBLIC_CASE_FIELDS}
    for k, v in result.items():
        if k == 'injection_candidates':
            if not isinstance(v, list) or not v or any(not isinstance(x, str) for x in v):
                raise ValueError('Invalid injection candidates')
        elif not isinstance(v, str) or not v:
            raise ValueError('Missing public case field: ' + k)
    return result


def public_packets(packets):
    """Only explicitly scoped public packets are accepted; no verdict passthrough."""
    result = []
    for i, packet in enumerate(packets):
        if not isinstance(packet, dict) or set(packet) != {
                'query_id', 'description', 'scope', 'response'}:
            raise ValueError('Unexpected public packet fields')
        if packet['scope'] == DELIVERY_SCOPE:
            validate_public_packet(packet)
            result.append(deepcopy(packet))
            continue
        response = packet['response']
        if (not isinstance(response, dict) or set(response) != {'supported', 'evidence'}
                or (response['supported'] is not None and type(response['supported']) is not bool)
                or not isinstance(response['evidence'], str)
                or packet['query_id'] != f'event_{i}'
                or packet['scope'] != 'returned_call_fields_only'
                or not isinstance(packet['description'], str)):
            raise ValueError('Invalid replay packet')
        try:
            evidence = json.loads(response['evidence'])
        except (ValueError, TypeError):
            evidence = None
        if isinstance(evidence, dict) and evidence.get('protocol') in (
                'active_tool_mediated_verification_v1', 'framework_assisted_target_verification_v2'):
            raise ValueError('Internal verification diagnostics are not attacker-visible evidence')
        result.append(deepcopy(packet))
    return result


@dataclass(frozen=True)
class PublicAttempt:
    case: dict
    strategy_document: str
    payload: Payload
    analysis: str
    final_output: str
    packets: tuple = ()
    origin: str = 'controlled_replay'
    error: str | None = None
    revision_plan: dict | None = None
    episode_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, 'case', public_case(self.case))
        object.__setattr__(self, 'packets', tuple(public_packets(self.packets)))
        report_episodes = {json.loads(packet['response']['evidence'])['episode_id']
                           for packet in self.packets if packet['scope'] == DELIVERY_SCOPE}
        if len(report_episodes) > 1:
            raise ValueError('An attempt cannot mix Probe Episodes')
        if self.episode_id is None and report_episodes:
            object.__setattr__(self, 'episode_id', next(iter(report_episodes)))
        if self.episode_id is not None:
            if not isinstance(self.episode_id, str) or not self.episode_id.strip():
                raise ValueError('Invalid Episode identity')
            if report_episodes and report_episodes != {self.episode_id}:
                raise ValueError('Probe report belongs to another Episode')
        expected = {packet['scope'] for packet in self.packets}
        if len(expected) > 1:
            raise ValueError('An attempt cannot mix observation protocols')
        if self.origin not in ('controlled_replay', 'target_execution'):
            raise ValueError('Invalid feedback origin')
        if not all(isinstance(v, str) for v in (self.analysis, self.final_output)):
            raise ValueError('Feedback text required')
        if not isinstance(self.strategy_document, str) or not self.strategy_document.strip():
            raise ValueError('Frozen strategy document required')

    def view(self, feedback_view):
        if feedback_view not in VIEWS:
            raise ValueError('Unknown feedback view')
        packets = []
        if feedback_view != 'final_only':
            expected = PACKET_SCOPES[feedback_view]
            if any(packet['scope'] != expected for packet in self.packets):
                raise ValueError('Feedback view does not permit these observation packets')
            packets = deepcopy(list(self.packets))
        return dict(case=deepcopy(self.case), strategy_document=self.strategy_document,
                    injection=self.payload.text, analysis=self.analysis,
                    final_output=self.final_output, origin=self.origin,
                    packets=packets, episode_id=self.episode_id)


@dataclass(frozen=True)
class AssessmentDecision:
    goal_status: str
    progress: str
    claim_support: str
    disclosure: str
    support_basis: str
    basis_refs: tuple
    unknowns: tuple
    action: str
    action_reason: str

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)


@dataclass
class BranchResult:
    status: str
    decisions: list = field(default_factory=list)
    generations: list = field(default_factory=list)
    target_attempts: int = 0
    error_type: str | None = None
    ablation: dict | None = None
