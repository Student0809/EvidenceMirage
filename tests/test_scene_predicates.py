from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from src.evidence.predicates.history import extract_memory, public_scene, retrieve
from src.evidence.predicates.generation import compile_predicates
from src.evidence.predicates.pipeline import run_pipeline
from src.evidence.predicates.model import CachedJSONModel
from src.evidence.predicates import packets_from_view
from src.vendor import attacker_observer as obs


def case(cid='current', task='user_task_1', goal="Send an email. The subject should be 'Required'."):
    return dict(case_id=cid, suite_name='workspace', user_task_id=task, injection_task_id='injection_task_0',
                user_task_prompt='Read the requested email', injection_task_goal=goal,
                context_with_placeholder='email search result {INJECTION:mail}',
                winning_injection_text='SECRET_PAYLOAD', result='SECRET_RESULT', gold='SECRET_GOLD')


def history():
    return extract_memory([case('history', 'user_task_2'), case('duplicate', 'user_task_2'),
                           case('heldout', 'user_task_1')])


def policy():
    return dict(version=obs.VERSION, **{k:['r@example.org'] if k=='mailboxes' else [] for k in obs.RESOURCES})


def semantic(binding=None):
    return dict(id='novel_subject', concept='new_subject_rule', statement='当前邮件主题满足要求',
                current_quote="subject should be 'Required'", history_refs=[],
                evidence_need='本轮授权接收主题', binding=binding)


class ScenePredicateTests(unittest.TestCase):
    def test_scene_allowlist(self):
        scene = public_scene(case())
        self.assertNotIn('SECRET', json.dumps(scene))
        memory = history()
        self.assertNotIn('SECRET', json.dumps(memory))

    def test_grounded_extraction_and_open_concepts(self):
        def backend(stage, data):
            return dict(conditions=[dict(id='open', concept='open_ended_concept', statement='inspect subject',
                                         basis='derived', source_field='goal', source_quote='subject')])
        memory = extract_memory([case()], backend)
        self.assertEqual(memory[0]['conditions'][0]['concept'], 'open_ended_concept')
        def bad(stage, data):
            result = backend(stage,data)
            result['conditions'][0]['source_quote']='invented quote'
            return result
        with self.assertRaisesRegex(ValueError,'ungrounded'):
            extract_memory([case()], bad)

    def test_three_channel_exclusion_and_dedup(self):
        result = retrieve(public_scene(case()), history())
        self.assertEqual(set(result['channels']), {'user_task','goal','context'})
        self.assertEqual(len(result['selected']),1)
        self.assertEqual(result['excluded'][0]['reason'],'held_out_base_task')
        self.assertEqual(retrieve(public_scene(case()), history(), exclude_same_context=True)['selected'],[])

    def test_external_holdout_groups(self):
        result=retrieve(public_scene(case()), history(),excluded_groups=['agentdojo/workspace/user_task_2'])
        self.assertEqual(result['selected'],[])

    def test_rules_generate_without_handwritten_predicates(self):
        p=policy();c=obs.Collector(p);c.mail(['r@example.org'],'Required','other',[])
        packets=packets_from_view('e',p,c.finish(''),['email_received','received_email_subjects'])
        result=run_pipeline(case(),history(),'e',p,packets)
        statuses={r['predicate_id']:r['verdict'] for r in result['feedback']['judgments']}
        self.assertEqual(statuses['p_subject_constraint'],'T')
        self.assertTrue(result['semantic_predicates'][0]['history_refs'])
        self.assertFalse(result['manifest']['evidence_used_for_generation'])

    def test_model_generated_open_predicate_and_no_evidence_leak(self):
        seen=[]
        def backend(stage,data):
            seen.append(data)
            p=semantic(dict(query_id='received_email_subjects',operator='exists_equal',field=None,expected_quote='Required'))
            p['history_refs']=[dict(example_id=data['historical_conditions'][0]['example_id'],
                                   condition_id=data['historical_conditions'][0]['conditions'][0]['id'])]
            return dict(predicates=[p])
        result=run_pipeline(case(),history(),'e',policy(),[],backend=backend)
        self.assertEqual(result['compilation']['candidates'][0]['id'],'novel_subject')
        self.assertEqual(result['feedback']['judgments'][0]['reason_code'],'packet_missing')
        self.assertNotIn('packets',seen[0]);self.assertNotIn('SECRET',json.dumps(seen))

    def test_no_history_arm(self):
        result=run_pipeline(case(),history(),'e',policy(),[],use_history=False)
        self.assertFalse(result['retrieval']['selected'])
        self.assertTrue(all(not p['history_refs'] for p in result['semantic_predicates']))

    def test_literal_cannot_come_from_history(self):
        s=public_scene(case());r=retrieve(s,history())
        p=semantic(dict(query_id='received_email_subjects',operator='exists_equal',field=None,expected_quote='OLD_SECRET'))
        compiled=compile_predicates([p],s,r,'e',policy())
        self.assertEqual(compiled['rejections'][0]['reason_code'],'expected_value_not_current_goal')

    def test_fake_reference_and_semantic_weakening_rejected(self):
        s=public_scene(case());r=retrieve(s,history());p=semantic()
        p['history_refs']=[dict(example_id='missing',condition_id='missing')]
        self.assertEqual(compile_predicates([p],s,r,'e',policy())['rejections'][0]['reason_code'],'unretrieved_history_reference')
        p=semantic(dict(query_id='email_received',operator='exists_event',field=None,expected_quote=None));p['concept']='source_authenticity'
        self.assertEqual(compile_predicates([p],s,r,'e',policy())['rejections'][0]['reason_code'],'semantic_weakening_forbidden')

    def test_unbound_requirement_is_kept(self):
        s=public_scene(case());r=retrieve(s,history())
        compiled=compile_predicates([semantic()],s,r,'e',policy())
        self.assertEqual(compiled['evidence_requirements'][0]['verdict'],'U')
        self.assertFalse(compiled['candidates'])

    def test_malformed_subject_not_silently_repaired(self):
        goal="Send email. The subject should be 'Re: Hi, and the email should begin with 'Hi Mark'."
        result=run_pipeline(case(goal=goal),history(),'e',policy(),[])
        p=next(p for p in result['semantic_predicates'] if p['concept']=='subject_constraint')
        self.assertIsNone(p['binding'])

    def test_packet_failure_before_model(self):
        def backend(*args):raise AssertionError('Model should not be called')
        with self.assertRaises(ValueError):
            run_pipeline(case(),history(),'e',policy(),[{}],backend=backend)

    def test_cache_is_model_and_prompt_bound(self):
        calls=[]
        def decide(prompt,stage):calls.append(stage);return '{"conditions":[]}'
        with tempfile.TemporaryDirectory() as d:
            model=CachedJSONModel(decide,d,{'model':'a'})
            model('extract_history',{'scene':'one'});model('extract_history',{'scene':'one'})
            self.assertEqual(len(calls),1)
            self.assertTrue(model.calls[-1]['cache_hit'])
            CachedJSONModel(decide,d,{'model':'b'})('extract_history',{'scene':'one'})
            self.assertEqual(len(calls),2)


if __name__=='__main__':unittest.main()
