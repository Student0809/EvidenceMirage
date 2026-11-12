from dataclasses import replace
import unittest
from src.engine.history import History, ObservationSummary
from src.policies.stage_aware import StageAware
from src.policies.stage_aware_no_evidence import StageAwareNoEvidence


class EvidenceAblationTests(unittest.TestCase):
    def history(self, triggers, hashes=None, stage='NOT_TRIGGERED', remaining=1):
        hashes = hashes or ['same'] * len(triggers)
        return History(tuple(ObservationSummary(str(i), sha, stage, t, 'Reject')
                             for i, (t, sha) in enumerate(zip(triggers, hashes))),
                       remaining, len(triggers)-1)

    def test_masked_controller_keeps_same_payload_retry_cap(self):
        h = self.history(['F', 'T', 'F'])
        self.assertEqual(StageAwareNoEvidence().decide(h).action.value, 'Revise')
        self.assertEqual(StageAwareNoEvidence().decide(h).reason, 'evidence_masked_payload_retry_cap')

    def test_false_evidence_changes_original_action_but_not_masked_action(self):
        h = self.history(['F'], remaining=3)
        self.assertEqual(StageAware().decide(h).action.value, 'Revise')
        self.assertEqual(StageAwareNoEvidence().decide(h).action.value, 'Retry')
        self.assertEqual(h.last.trigger, 'F')

    def test_new_payload_does_not_inherit_preceding_payload_retry_cap(self):
        h = self.history(['F', 'F', 'F'], ['P0', 'P0', 'P1'])
        self.assertEqual(StageAwareNoEvidence().decide(h).action.value, 'Retry')

    def test_common_stop_rules_are_preserved(self):
        for h in (self.history(['T'], stage='SUCCESS'), self.history(['U'], stage='ERROR'),
                  self.history(['F'], remaining=0)):
            self.assertEqual(StageAwareNoEvidence().decide(h), StageAware().decide(h))


if __name__ == '__main__':
    unittest.main()
