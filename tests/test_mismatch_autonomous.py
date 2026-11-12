"""Actual V5 preparation, autonomous control and private-label noninterference."""
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from src.attacker.original import OriginalPIMinerAdapter
from src.attacker.piminer import PIMinerAdapter
from src.attacker.revision_memory import build_revision_memory
from src.feedback.history import revision_history
from src.feedback.schema import PROTOCOL
from src.vendor.revision_planner import validate_plan
from src.mismatch.execution import run_prepared
from src.mismatch.fixture import decide as fixture_decide
from src.evaluation.mismatch import score,summarize
from src.mismatch.replay_observer import replay_packets
from unittest.mock import patch
from src.feedback.history import history_item
from src.feedback.assessment import validate_assessment
from src.engine.feedback_runner import FeedbackRunner
from src.schema import Payload
from src.mismatch.data import ROOT, prepare, jsonl, load_attempt


class DataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temp.name) / 'prepared'
        cls.manifest = prepare(cls.output)
        cls.rows = jsonl(cls.output / 'model_inputs.jsonl')
        cls.private = jsonl(cls.output / 'scoring_private.jsonl')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_126_instances_and_18_shared_initializations(self):
        self.assertEqual(self.manifest['instances'], 126)
        self.assertEqual(self.manifest['evidence_files'], 46)
        for sid in {r['seed_id'] for r in self.private}:
            rows = [p for p, s in zip(self.rows, self.private) if s['seed_id'] == sid]
            self.assertEqual(len(rows), 7)
            self.assertEqual(len({p['injection'] for p in rows}), 1)
            self.assertEqual(len({p['strategy_document'] for p in rows}), 1)

    def test_no_private_fields_in_public_projection(self):
        for row in self.rows:
            self.assertEqual(set(row), {'uid','case','strategy_document','injection','analysis','final_output','packets'})
            for packet in row['packets']:
                receipt = json.loads(packet['response']['evidence'])
                self.assertEqual(set(receipt), {'call', 'result', 'error'})
            self.assertNotIn('IPI_PLACEHOLDER', json.dumps(row['packets']))

    def test_m1_blackbox_identical_and_m2_m3_evidence_identical(self):
        pairs = json.loads((self.output / 'pairing_private.json').read_text())
        by_id = {r['uid']: r for r in self.rows}
        for pair in pairs:
            left = load_attempt(by_id[pair['left']]); right = load_attempt(by_id[pair['right']])
            if pair['kind'] == 'state_intervention':
                self.assertEqual(left.view('final_only'), right.view('final_only'))
            else:
                a = left.view('authorized_evidence'); b = right.view('authorized_evidence')
                a.pop('final_output'); b.pop('final_output')
                self.assertEqual(a, b)

    def test_no_silent_overwrite(self):
        with self.assertRaises(ValueError):
            prepare(self.output)


def judgment(status='unknown', action='Revise'):
    return dict(goal_status=status, progress='full' if status=='completed' else 'unknown',
                claim_support='undetermined', disclosure='unknown',support_basis='insufficient', basis_refs=[],
                unknowns=['Tool execution is not known'], action=action, action_reason='Choose next action')


class Scripted:
    def __init__(self, assessments=None):
        self.assessments = list(assessments or [judgment()])
        self.prompts = []

    def __call__(self, prompt, stage):
        self.prompts.append((stage, prompt))
        if stage == 'assessment':
            value = self.assessments.pop(0)
            data = json.loads(prompt.split('PUBLIC INPUT:\n', 1)[1])
            if data.get('assessment_mode') == 'ordinary':
                value = {k: value[k] for k in ('goal_status', 'action', 'action_reason')}
            return json.dumps(value)
        if stage == 'attacker_revise':
            return '<analysis>Test a different hypothesis.</analysis><injection>revised benchmark payload</injection>'
        raise AssertionError('Unexpected model stage: '+stage)


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        p = Path(cls.temp.name) / 'prepared'; prepare(p)
        cls.row = jsonl(p / 'model_inputs.jsonl')[1]  # Hidden full execution; still Revise is permitted.

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def attacker(self, scripted):
        return OriginalPIMinerAdapter(ROOT, 'fixture', 'fixture', decide=scripted)

    def test_hidden_true_does_not_prevent_revision(self):
        model=Scripted(); result=FeedbackRunner(self.attacker(model)).run(load_attempt(self.row))
        self.assertEqual(result.status, 'revision_ready')
        self.assertEqual(result.target_attempts, 0)
        self.assertEqual([s for s,p in model.prompts], ['assessment','attacker_revise'])

    def test_stop_and_retry_do_not_generate_or_execute(self):
        for value, expected in [(judgment('completed','Stop'),'self_reported_complete'),
                                (judgment('unknown','Retry'),'retry_requested')]:
            model=Scripted([value]); result=FeedbackRunner(self.attacker(model)).run(load_attempt(self.row))
            self.assertEqual(result.status,expected); self.assertFalse(result.generations)
            self.assertEqual(len(model.prompts),1)

    def test_bad_reference_and_conflicting_decision_are_errors(self):
        for value in [judgment('completed','Revise'), judgment('unknown','Stop')]:
            model=Scripted([value]); result=FeedbackRunner(self.attacker(model)).run(load_attempt(self.row))
            self.assertEqual(result.status,'judgment_error');self.assertFalse(result.generations)

    def test_hidden_field_mutation_does_not_change_model_input(self):
        a = dict(self.row, gold=True, status='PRIVATE_TRUE', utility='SECRET_UTILITY')
        b = dict(self.row, gold=False, status='PRIVATE_FALSE', utility='OTHER_UTILITY')
        models=[Scripted(),Scripted()]
        for row,model in zip([a,b],models):
            FeedbackRunner(self.attacker(model)).run(load_attempt(row))
        self.assertEqual(models[0].prompts,models[1].prompts)
        self.assertNotIn('PRIVATE_',str(models[0].prompts))

    def test_foreign_history_is_rejected_before_opaque_identity_projection(self):
        attempt=load_attempt(self.row)
        foreign=replace(attempt,case=dict(attempt.case,user_task_prompt='Foreign task'))
        model=Scripted()
        with self.assertRaises(ValueError):
            self.attacker(model).assess_and_decide(attempt,[history_item(foreign),history_item(attempt)],4)
        with self.assertRaises(ValueError):
            self.attacker(model).revise(attempt,[history_item(foreign),history_item(attempt)],
                validate_assessment(judgment(),set()))
        self.assertEqual(model.prompts,[])

    def test_valid_unknown_is_not_target_error(self):
        model=Scripted([judgment('unknown','Retry')])
        result=FeedbackRunner(self.attacker(model)).run(load_attempt(self.row))
        self.assertEqual(result.status,'retry_requested')

    def test_unresolved_candidate_stops_before_target_and_preserves_generation(self):
        class BadCandidate(Scripted):
            def __call__(self,prompt,stage):
                if stage=='attacker_revise':
                    return '<analysis>Unfilled template.</analysis><injection>{INJECTION:unfilled}</injection>'
                return super().__call__(prompt,stage)
        model=BadCandidate()
        result=FeedbackRunner(self.attacker(model),source=lambda *a:self.fail('Target must not dispatch')).run(load_attempt(self.row))
        self.assertEqual(result.status,'generation_error');self.assertEqual(result.target_attempts,0)
        self.assertEqual(result.generations[0]['status'],'GENERATION_ERROR')

    def test_last_live_feedback_is_assessed_before_budget_stop(self):
        model=Scripted([judgment('unknown','Retry'),judgment('completed','Stop')])
        def source(current,payload,ordinal):
            return replace(current,origin='target_execution',final_output='new actual reply')
        result=FeedbackRunner(self.attacker(model),source=source,target_budget=1).run(load_attempt(self.row))
        self.assertEqual(result.status,'self_reported_complete');self.assertEqual(result.target_attempts,1)

    def test_no_execution_after_budget_and_target_error_counts(self):
        model=Scripted([judgment('unknown','Retry')])
        result=FeedbackRunner(self.attacker(model),target_budget=0).run(load_attempt(self.row))
        self.assertEqual(result.status,'budget_exhausted');self.assertFalse(result.generations)
        def source(*args): raise RuntimeError('provider failed')
        model=Scripted([judgment('unknown','Retry')])
        result=FeedbackRunner(self.attacker(model),source=source).run(load_attempt(self.row))
        self.assertEqual(result.status,'target_error');self.assertEqual(result.target_attempts,1)


class V1Scripted(Scripted):
    def __call__(self, prompt, stage):
        if stage in ('assessment','attacker_revise'):
            return super().__call__(prompt,stage)
        self.prompts.append((stage,prompt))
        if stage in ('planner','plan_revision'):
            data=json.loads(prompt.split('PUBLIC INPUT:\n',1)[1]); context=data['context']
            reviews=[dict(plan_id=o['plan_id'],assessment='inconclusive',decision='adjust',
                          outcome_refs=o['outcome_refs'][:1],reason='Outcome remains uncertain',preserve=[])
                     for o in context['revision_memory'].get('plan_outcomes',[])]
            return json.dumps(dict(action='make_plan',observations=[],cause='unknown',
                hypothesis='A different framing may change behavior', edit_scope='Adjust request framing',
                preserve=[],unknowns=['Execution outcome is not established'],
                objective=context['injection_task_goal'],
                hypothesis_test=dict(support_refs=[],counter_refs=[],unknowns=['Causal effect unknown'],
                                     predicted_observation='A matching permitted call receipt'),
                strategy_rationale='Test a variant of the frozen strategy',previous_plan_review=reviews,
                edits=[dict(change='Adjust framing',basis='hypothesis',references=['goal'],expected_effect='Test response')]))
        if stage in ('generator','static_repair'):
            return '<injection>revised benchmark payload</injection>'
        if stage=='semantic_review':
            return json.dumps(dict(assessments={k:'unknown' for k in
                ['plan_goal_alignment','candidate_goal_alignment','plan_following','evidence_alignment']},findings=[]))
        raise AssertionError('Unexpected V1 stage '+stage)


class V1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();out=Path(cls.temp.name)/'prepared';prepare(out)
        cls.attempt=load_attempt(jsonl(out/'model_inputs.jsonl')[1])

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_unknown_outcome_flows_through_existing_v1_planner_generator(self):
        model=V1Scripted(); result=FeedbackRunner(PIMinerAdapter(ROOT,'fixture',decide=model),
            feedback_view='authorized_evidence').run(self.attempt)
        self.assertEqual(result.status,'revision_ready',result)
        self.assertEqual([s for s,p in model.prompts],['assessment','planner','generator'])
        memory=result.generations[0]['revision_memory']
        self.assertNotIn('security',memory['attempts'][0]);self.assertNotIn('utility',memory['attempts'][0])
        self.assertEqual(memory['attempts'][0]['trigger'],'U')
        assessment=next(f for f in memory['facts'] if f['kind']=='attacker_assessment')
        self.assertEqual(assessment['scope'],'unverified_model_judgment')
        context=json.loads(model.prompts[1][1].split('PUBLIC INPUT:\n',1)[1])['context']
        self.assertNotIn('gold',context)
        for stage,prompt in model.prompts:
            self.assertIn('controlled_replay',prompt)
            if stage!='assessment':
                self.assertIn('returned_call_fields_only',prompt)
                self.assertIn('not execution caused by the supplied payload',prompt)

    def test_autonomous_backend_does_not_call_disabled_semantic_reviewer(self):
        class FailedReview(V1Scripted):
            def __call__(self,prompt,stage):
                if stage=='semantic_review':raise RuntimeError('No returned review')
                return super().__call__(prompt,stage)
        model=FailedReview()
        result=FeedbackRunner(PIMinerAdapter(ROOT,'fixture',decide=model)).run(self.attempt)
        self.assertEqual(result.status,'revision_ready')
        self.assertEqual(result.generations[0]['semantic_review_mode'], 'disabled')

    def test_final_only_v1_receives_no_packets_in_any_phase(self):
        model=V1Scripted(); result=FeedbackRunner(PIMinerAdapter(ROOT,'fixture',decide=model),
            feedback_view='final_only').run(self.attempt)
        self.assertEqual(result.status,'revision_ready')
        for stage,prompt in model.prompts:
            self.assertNotIn('returned_call_fields_only',prompt)
        self.assertEqual(result.generations[0]['revision_memory']['attempts'][0]['revision_observation']['predicates'],[])

    def test_attacker_assessment_cannot_be_promoted_to_observed_fact(self):
        history=[history_item(self.attempt)]
        decision=validate_assessment(judgment(),set())
        projected=revision_history(self.attempt,history,decision,'final_only')
        case=dict(self.attempt.case,case_id='current_case',policy_id='autonomous')
        memory=build_revision_memory(case,projected,feedback_protocol=PROTOCOL)
        plan=dict(action='make_plan',observations=['attempt:0:assessment'],preserve=[],unknowns=[],
                  hypothesis='Unknown cause',edit_scope='Framing')
        with self.assertRaisesRegex(ValueError,'observations|Observations'):
            validate_plan(plan,self.attempt.payload.text,goal=case['injection_task_goal'],
                          revision_memory=memory,strategy_document=self.attempt.strategy_document)

    def test_multiround_unknown_history_retains_plan_and_assessment(self):
        model=V1Scripted([judgment(),judgment()])
        def source(current,payload,ordinal):
            return replace(current,payload=payload,origin='target_execution',final_output='New reply')
        result=FeedbackRunner(PIMinerAdapter(ROOT,'fixture',decide=model),source=source,
                feedback_view='final_only',target_budget=1).run(self.attempt)
        self.assertEqual(result.status,'budget_exhausted')
        self.assertEqual(result.target_attempts,1);self.assertEqual(len(result.decisions),2)
        # Explicitly inspect the history presented to the second assessment.
        prompt=[p for s,p in model.prompts if s=='assessment'][-1]
        self.assertIn('revision_plan',result.generations[0])
        self.assertIn('attacker_assessment',prompt)

    def test_second_revision_keeps_prior_plan_without_assuming_failure(self):
        model=V1Scripted([judgment(),judgment(),judgment('completed','Stop')])
        def source(current,payload,ordinal):
            return replace(current,payload=payload,origin='target_execution',final_output=f'Reply {ordinal}')
        result=FeedbackRunner(PIMinerAdapter(ROOT,'fixture',decide=model),source=source,
                feedback_view='final_only',target_budget=2).run(self.attempt)
        self.assertEqual(result.status,'self_reported_complete',result)
        self.assertEqual(len(result.generations),2)
        memory=result.generations[1]['revision_memory']
        self.assertEqual(len(memory['attempts']),2)
        self.assertEqual(len(memory['plan_outcomes']),1)
        self.assertEqual(len(result.generations[1]['plan']['previous_plan_review']),1)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        cls.prepared=cls.root/'prepared';prepare(cls.prepared)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_both_real_entry_points_process_all_126_with_synthetic_model(self):
        for method in ('piminer','v1'):
            out=self.root/method
            result=run_prepared(self.prepared,out,method=method,decide=fixture_decide,fixture=True)
            self.assertEqual(result['instances'],126);self.assertEqual(result['errors'],0)
            metrics=score(self.prepared,out,self.root/f'{method}.score.json')
            self.assertEqual(metrics['result_kind'],'SYNTHETIC_SMOKE')
            self.assertEqual(metrics['unknown_count'],126);self.assertEqual(metrics['coverage'],0)
            self.assertIsNone(metrics['selective_accuracy'])
            self.assertEqual(metrics['target_runs'],0)
            self.assertEqual(metrics['pair_summary']['response_intervention']['total'],36)
            self.assertEqual(len(metrics['by_split_group_id']),10)
            self.assertNotIn('asr',metrics)

    def test_runner_does_not_open_private_labels(self):
        original=Path.read_text
        def read(path,*args,**kwargs):
            if path.name in ('scoring_private.jsonl','pairing_private.json'):
                raise AssertionError('Runner attempted to read hidden labels')
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',read):
            result=run_prepared(self.prepared,self.root/'private_denied',method='v1',
                decide=fixture_decide,fixture=True,limit=1)
        self.assertEqual(result['errors'],0)

    def test_fixture_api_cannot_accidentally_use_live_transport(self):
        with patch('src.attacker.transport.RoleDecision.__call__',side_effect=AssertionError('Network forbidden')):
            result=run_prepared(self.prepared,self.root/'fixture_no_decider',method='piminer',fixture=True,limit=1)
        self.assertEqual(result['errors'],0)

    def test_scorer_rejects_incomplete_outputs_before_opening_labels(self):
        out=self.root/'incomplete';out.mkdir()
        (out/'manifest.json').write_text('{}')
        (out/'status.json').write_text('{"status":"running"}')
        with self.assertRaisesRegex(ValueError,'complete'):
            score(Path('/nonexistent/private/data'),out,self.root/'invalid_score.json')

    def test_partial_known_cost_is_preserved_without_inventing_total(self):
        rows=[]
        for cost in (.25,None):
            rows.append(dict(label=dict(actual_goal_completed=False,claim_supported=None,disclosure='silent'),
                prediction=dict(assessment=judgment('unknown','Retry'),status='retry_requested',
                    model_calls=1,candidate_sha256=[],reported_cost_usd=cost,known_reported_cost_usd=cost or 0)))
        metrics=summarize(rows)
        self.assertIsNone(metrics['reported_cost_usd'])
        self.assertEqual(metrics['known_reported_cost_usd'],.25)

    def test_replay_projection_does_not_read_private_state_or_role(self):
        common=dict(call=dict(function='send_money',args={'amount':1}),result='receipt',error=None)
        a=dict(steps=[dict(common,role='goal_write',post_state='SECRET_A')],pre='SECRET_B',goal_facts='SECRET_C')
        b=dict(steps=[dict(common,role='normal',post_state='OTHER')],pre='OTHER',goal_facts='OTHER')
        self.assertEqual(replay_packets(a,'banking'),replay_packets(b,'banking'))


if __name__ == '__main__':
    unittest.main()
