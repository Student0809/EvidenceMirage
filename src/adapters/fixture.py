"""Deterministic engineering fixtures. These are NOT measured AgentDojo ASR."""
from ..schema import TargetResult, Payload
from ..evidence.observer import policy_for_case
from ..vendor.attacker_observer import Collector


class FixtureTarget:
    def __init__(self):
        self.counts = {}

    def execute(self, case, payload, episode_id):
        arm = episode_id.split('/')[1]
        key = case['case_id'], arm
        number = self.counts.get(key, 0)
        self.counts[key] = number + 1
        collector = Collector(policy_for_case(case))
        mode = int(case['case_id'].lstrip('c')) % 5
        gold = (arm != 'shared' and number == 2 and mode != 4) or (arm == 'shared' and mode == 3)
        if mode == 4 and arm != 'shared' and number == 1:
            return TargetResult(None, error='FixtureTargetError')
        if mode in (0, 1) and (number == 0 or mode == 0):
            for mailbox in collector.policy['mailboxes']:
                collector.mail([mailbox], 'Fixture', 'Synthetic receipt', ['file'] if number == 0 else [])
        final = 'I cannot comply.' if mode == 0 else 'Completed simulated action.'
        return TargetResult(gold, final=final, observation=collector.finish(final),
                            raw=dict(kind='SYNTHETIC_FIXTURE', case_id=case['case_id']))


class FixtureAttacker:
    def generate(self, case, payload, episode, packets, revision_id):
        text = (payload.text if payload else 'Engineering fixture') + '\nFixture revision ' + revision_id
        return Payload(revision_id, text), dict(status='ready', calls=[], checks=[], kind='SYNTHETIC_FIXTURE')
