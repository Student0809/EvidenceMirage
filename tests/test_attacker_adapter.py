from dataclasses import replace
import json
import unittest
from src.attacker.piminer import PIMinerAdapter, GenerationError
from src.engine.config import ROOT, load_cases, load_config
from src.schema import Payload
from src.evidence.observer import FixedObserver, policy_for_case
from src.vendor.attacker_observer import Collector
from src.vendor.revision_checker import SEMANTIC_DIMENSIONS
from types import SimpleNamespace


class AttackerTests(unittest.TestCase):
    def setUp(self):
        self.case=load_cases(load_config(ROOT/'experiments/e0_smoke.yaml'))[0]
        self.case['private_judge']='PRIVATE_SENTINEL'
        self.case['future_history']=['FUTURE_SENTINEL']
        collector=Collector(policy_for_case(self.case))
        collector.mail(collector.policy['mailboxes'],'x','y',[])
        self.packets=FixedObserver().collect('h1',self.case,collector.finish('I refuse.'))
        self.episode=SimpleNamespace(gold=False,error=None,final='I refuse.',episode_id='h1')
        self.prompts=[]

    def plan(self):
        return dict(action='make_plan',observations=[],hypothesis='Bounded revision',edit_scope='Payload text',
                    preserve=[],unknowns=[],objective=self.case['injection_task_goal'],
                    cause='unknown', strategy_rationale='Retain the selected strategy and vary phrasing.',
                    hypothesis_test=dict(support_refs=['goal'], counter_refs=[],
                        unknowns=['The cause is not established'],
                        predicted_observation='The authorized goal event may appear.'),
                    edits=[dict(change='Change phrasing',basis='hypothesis',references=['goal'],expected_effect='Goal compliance')])

    def decide(self,prompt,stage):
        self.prompts.append((stage,prompt))
        if stage=='planner':return json.dumps(self.plan())
        if stage=='semantic_review':
            return dict(assessments={k:'unknown' for k in SEMANTIC_DIMENSIONS},findings=[])
        return '<injection>revised candidate</injection>'

    def test_real_planner_generator_checker_with_no_active_query_or_private_input(self):
        a=PIMinerAdapter(ROOT,'fixture',decide=self.decide)
        payload,record=a.generate(self.case,Payload('P0','old'),self.episode,self.packets,'P1')
        self.assertEqual(payload.text,'revised candidate')
        self.assertEqual([c['stage'] for c in record['calls']],['planner','generator','semantic_review'])
        self.assertEqual(record['checks'][0]['status'],'REVIEW')
        for stage,prompt in self.prompts:
            self.assertNotIn('PRIVATE_SENTINEL',prompt)
            self.assertNotIn('FUTURE_SENTINEL',prompt)
            if stage=='planner':self.assertIn('"allow_query": false',prompt)

    def test_one_repair_and_terminal_failure(self):
        def decide(prompt,stage):
            return json.dumps(self.plan()) if stage=='planner' else 'no injection block'
        a=PIMinerAdapter(ROOT,'fixture',decide=decide)
        with self.assertRaises(GenerationError) as caught:
            a.generate(self.case,Payload('P0','old'),self.episode,self.packets,'P1')
        r=caught.exception.record
        self.assertEqual([c['stage'] for c in r['calls']],['planner','generator','static_repair'])
        self.assertEqual(len(r['checks']),2)

    def test_gold_success_cannot_be_revised(self):
        self.episode.gold=True
        a=PIMinerAdapter(ROOT,'fixture',decide=self.decide)
        with self.assertRaises(GenerationError):
            a.generate(self.case,Payload('P0','old'),self.episode,self.packets,'P1')
        self.assertEqual(self.prompts,[])


if __name__=='__main__':unittest.main()
