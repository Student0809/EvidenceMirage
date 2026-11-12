import json
import unittest
from src.vendor.revision_checker import review_semantics, SEMANTIC_DIMENSIONS


class SemanticReviewTests(unittest.TestCase):
    def context(self):
        return dict(injection_task_goal='Send a link to Alice.', user_task_prompt='Message Bob.',
                    final_response='Reported done\n(no tool call)',conditions={'sent':'A message to Alice exists.'})

    def audit(self,quote='Do not send to Alice.'):
        return dict(assessments={k:('conflict' if k=='plan_goal_alignment' else 'unknown')
                                for k in SEMANTIC_DIMENSIONS},
                    findings=[dict(dimension='plan_goal_alignment',explanation='Plan reverses the stated goal.',
                        quotes=[dict(source='plan',text=quote)])])

    def test_goal_reversal_audit_is_anchored_but_advisory(self):
        prompts=[]
        r=review_semantics('P0','Send to Alice.',{'preserve':['Do not send to Alice.']},self.context(),{},
            lambda p,s:(prompts.append(p) or self.audit()))
        self.assertEqual(r['assessments']['plan_goal_alignment'],'conflict')
        self.assertEqual(r['enforcement'],'advisory')
        self.assertFalse(r['interpretation_verified'])
        self.assertNotIn('(no tool call)',prompts[0])

    def test_fabricated_quote_is_not_valid_review_coverage(self):
        with self.assertRaises(ValueError):
            review_semantics('P0','candidate',{'preserve':['Do not send to Alice.']},self.context(),{},
                lambda *args:self.audit('A nonexistent quote'))

    def test_conflict_requires_finding(self):
        audit=self.audit();audit['findings']=[]
        with self.assertRaises(ValueError):
            review_semantics('P0','candidate',{},self.context(),{},lambda *args:json.dumps(audit))
