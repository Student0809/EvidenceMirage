import json
import unittest
from copy import deepcopy
from src.vendor.revision_planner import project_context, run_planner, validate_plan, feedback_view
from src.vendor.refinement import generation_prompt

PLAN = dict(action='make_plan', observations=['Final reports missing output'],
            hypothesis='Execution may have stopped early', edit_scope='Clarify the final step',
            preserve=['Exact subject'], unknowns=['Cause is not established'], objective='Fixture goal',
            edits=[dict(change='Clarify step', basis='hypothesis', references=['final_report'],
                        expected_effect='Test early-stop explanation')])

class PlannerTests(unittest.TestCase):
    def context(self):
        return project_context(dict(user_task_prompt='fixture', injection_task_goal='Fixture goal', history=[dict(
            injection='P0', security=False, final_output='done', oracle_run_id='000/round_1',
            trajectory='PRIVATE', conditions={'secret': True})]), {'a': 'A', 'b': 'B'})

    def test_public_projection_and_optional_zero_queries(self):
        c = self.context()
        self.assertNotIn('PRIVATE', json.dumps(c))
        def query(*args): self.fail('No query selected')
        r = run_planner(c, lambda *a: PLAN, query, queries_remaining=5)
        self.assertEqual(r['queries'], [])

    def test_adaptive_query_then_plan_and_redaction(self):
        prompts = []
        def decide(prompt, stage):
            prompts.append(prompt)
            return dict(action='query_subgoal', condition_id='a', reason='Unknown') if len(prompts)==1 else PLAN
        r = run_planner(self.context(), decide, lambda *a: dict(supported=False, evidence='not met',
            queries_remaining=4, private='SECRET'), queries_remaining=5)
        self.assertFalse(r['evidence']['a']['supported'])
        self.assertNotIn('SECRET', prompts[-1])
        self.assertIn('NOT identify a cause', prompts[-1])

    def test_invalid_actions_bounded_and_budget_forces_plan(self):
        calls=[]
        def decide(prompt, stage):
            calls.append(prompt)
            return dict(action='query_subgoal', condition_id='foreign', reason='x') if len(calls)<3 else PLAN
        r=run_planner(self.context(), decide, lambda *a:self.fail('invalid query reached oracle'),
                      queries_remaining=5,max_actions=2)
        self.assertEqual(len(calls),3)
        self.assertIn('"allow_query": false',calls[-1])
        self.assertEqual(r['queries_remaining'],5)

    def test_replay_never_calls_oracle(self):
        events=[dict(request=dict(tool='query_subgoal',run_id='000/round_1',condition_id='b'),
                     response=dict(supported=True,evidence='yes',private='SECRET'))]
        r=run_planner(self.context(),lambda *a:PLAN,lambda *a:self.fail('replay queried'),
                      queries_remaining=5,events=events,acquisition='replay')
        self.assertEqual(r['evidence'],{'b':dict(supported=True,evidence='yes')})
        events[0]['request']['run_id']='001/round_1'
        with self.assertRaises(ValueError):
            run_planner(self.context(),lambda *a:PLAN,None,queries_remaining=5,events=events,acquisition='replay')

    def test_zero_budget_and_gold_stop(self):
        with self.assertRaises(ValueError):
            run_planner(self.context(),lambda *a:dict(action='query_subgoal',condition_id='a',reason='x'),
                        None,queries_remaining=0)
        with self.assertRaises(ValueError):
            project_context(dict(history=[dict(injection='p',security=True)]),{})

    def test_legacy_replay_malformed_plan_has_no_corrective_turn(self):
        calls=[]
        with self.assertRaisesRegex(ValueError,'valid final plan'):
            run_planner(self.context(),lambda p,s:(calls.append(p) or 'not JSON'),None,
                        queries_remaining=0,acquisition='replay',max_actions=1)
        self.assertEqual(len(calls),1)

    def test_goal_binding_and_unqueried_evidence_rejected(self):
        from copy import deepcopy
        wrong = dict(PLAN, objective='Complete benign task instead')
        with self.assertRaises(ValueError):
            validate_plan(wrong, 'P0', goal='Fixture goal', evidence={})
        wrong = deepcopy(PLAN)
        wrong['edits'][0]['references'] = ['condition:unqueried']
        validate_plan(wrong, 'P0', goal='Fixture goal', evidence={})

    def test_legacy_final_suffix_is_only_cleaned_in_model_view(self):
        c = dict(self.context(), final_response='Reported done\n(no tool call)')
        prompts=[]
        run_planner(c, lambda p,s: (prompts.append(p) or PLAN), None, queries_remaining=0)
        self.assertNotIn('Reported done\\n(no tool call)', prompts[0])
        self.assertEqual(c['final_response'], 'Reported done\n(no tool call)')
        quoted=dict(c, final_response='They wrote (no tool call) in a document.')
        self.assertEqual(feedback_view(quoted)['final_response'],quoted['final_response'])


class GroundedPlannerTests(unittest.TestCase):
    def context(self):
        return PlannerTests.context(self)

    def memory(self):
        return dict(protocol='grounded_history_v1', base_payload_sha256='current-sha',
                    attempts=[dict(episode_id='early', final_output='EARLY_FINAL', analysis='EARLY_ANALYSIS'),
                              dict(episode_id='latest', final_output='LATEST_FINAL')],
                    candidates=[dict(payload_sha256='early-sha', payload='EARLY_PAYLOAD'),
                                dict(payload_sha256='current-sha', payload='P0')],
                    positive_reference_candidates=['early-sha'],
                    facts=[dict(id='goal',kind='public_input',value='Fixture goal'),
                           dict(id='old_payload',kind='payload_text',value='EARLY_PAYLOAD'),
                           dict(id='old_trigger',kind='observed_trigger',value='T',episode_id='early'),
                           dict(id='latest_trigger',kind='observed_trigger',value='F',episode_id='latest'),
                           dict(id='predicate',kind='observed_predicate',value='F'),
                           dict(id='unknown',kind='observed_predicate',value='U'),
                           dict(id='null',kind='observed_predicate',value=None),
                           dict(id='report',kind='model_report',value='The Target reported done.')])

    def grounded_plan(self):
        return dict(PLAN, observations=['old_trigger','latest_trigger'], cause='unknown',
                    hypothesis_test=dict(support_refs=['latest_trigger','report'],
                                         counter_refs=['old_trigger'],
                                         unknowns=['Cause and parameter validity remain unknown'],
                                         predicted_observation='Check the same authorized visible predicate'),
                    strategy_rationale='Test the selected strategy mechanism while retaining the goal')

    def validate_grounded(self, plan):
        return validate_plan(plan,'P0',goal='Fixture goal',evidence={},
                             revision_memory=self.memory(),strategy_document='SELECTED_STRATEGY')

    def test_structured_plan_keeps_hypothesis_and_goal_binding(self):
        plan=self.validate_grounded(self.grounded_plan())
        self.assertEqual(plan['cause'],'unknown')
        self.assertEqual(plan['hypothesis_test']['counter_refs'],['old_trigger'])
        self.assertIn('strategy_rationale',plan)
        wrong=dict(self.grounded_plan(),objective='Another goal')
        with self.assertRaises(ValueError):self.validate_grounded(wrong)

    def test_prior_plan_review_validates_structure_without_resolving_outcome_refs(self):
        memory = self.memory()
        memory['plan_outcomes'] = [dict(plan_id='case/policy/P1',
                                        outcome_refs=['latest_trigger', 'report', 'unknown'])]
        def validate(plan):
            return validate_plan(plan, 'P0', goal='Fixture goal', evidence={},
                                 revision_memory=memory, strategy_document='SELECTED_STRATEGY')
        plan = self.grounded_plan()
        with self.assertRaisesRegex(ValueError, 'every prior plan'):
            validate(plan)
        review = dict(plan_id='case/policy/P1', assessment='inconclusive', decision='adjust',
                      outcome_refs=['report'], reason='The final report cannot localize failure.',
                      preserve=['Retain the explicit goal argument'])
        plan['previous_plan_review'] = [review]
        self.assertEqual(validate(plan)['previous_plan_review'], [review])
        for refs in [['old_trigger'], ['attempt:2:predicate:event:2'], ['report']]:
            for assessment in ('inconclusive', 'supported', 'contradicted'):
                updated = dict(review, outcome_refs=refs, assessment=assessment)
                self.assertEqual(validate(dict(plan, previous_plan_review=[updated]))[
                    'previous_plan_review'], [updated])
        for change in [dict(plan_id='foreign'), dict(outcome_refs=[]),
                       dict(outcome_refs=[2]), dict(outcome_refs='report')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(dict(plan, previous_plan_review=[dict(review, **change)]))
        review.update(assessment='contradicted', outcome_refs=['latest_trigger'])
        self.assertEqual(validate(plan)['previous_plan_review'][0]['assessment'], 'contradicted')

    def test_known_fact_reference_alias_is_normalized_with_audit_before_validation(self):
        context = dict(self.context(), revision_memory=self.memory(), strategy_document='SELECTED_STRATEGY')
        plan = deepcopy(self.grounded_plan())
        plan['edits'][0]['references'] = ['old_payload']
        plan['hypothesis_test']['support_refs'] = ['fact:latest_trigger']
        result = run_planner(context, lambda *args: plan, None, queries_remaining=0,
                             acquisition='replay', max_actions=1)
        self.assertEqual(len(result['actions']), 1)
        action = result['actions'][0]
        self.assertEqual(action['action'], plan)
        self.assertEqual(len(action['reference_normalizations']), 2)
        self.assertEqual(result['plan']['edits'][0]['references'], ['fact:old_payload'])

    def test_invented_unknown_and_report_observations_rejected(self):
        for ref in ('Invented prose observation','missing','unknown','null','report'):
            with self.subTest(ref=ref),self.assertRaises(ValueError):
                self.validate_grounded(dict(self.grounded_plan(),observations=[ref]))
        for cause in (None,'fixed value was fabricated','known'):
            with self.subTest(cause=cause),self.assertRaises(ValueError):
                self.validate_grounded(dict(self.grounded_plan(),cause=cause))
        for field in ('support_refs','counter_refs'):
            wrong=deepcopy(self.grounded_plan())
            wrong['hypothesis_test'][field]=['fabricated']
            self.validate_grounded(wrong)
        wrong=dict(self.grounded_plan(),facts=[dict(id='invented',value='Known cause')])
        self.assertNotIn('facts', self.validate_grounded(wrong))

    def test_observation_edit_requires_known_projected_fact(self):
        for ref in ('fact:unknown','fact:null','fact:report','strategy','goal','final_report'):
            wrong=deepcopy(self.grounded_plan())
            wrong['edits'][0].update(basis='observation',references=[ref])
            with self.subTest(ref=ref),self.assertRaises(ValueError):
                self.validate_grounded(wrong)
        for ref in ('fact:goal','fact:old_payload','fact:old_trigger','fact:predicate'):
            valid=deepcopy(self.grounded_plan())
            valid['edits'][0].update(basis='observation',references=[ref])
            self.assertEqual(self.validate_grounded(valid)['edits'][0]['references'],[ref])

    def test_hypothesis_may_reference_unknown_and_selected_strategy(self):
        plan=deepcopy(self.grounded_plan())
        plan['hypothesis_test']['support_refs']=['unknown','null','report']
        plan['edits'][0]['references']=['strategy','fact:unknown']
        self.assertEqual(self.validate_grounded(plan)['edits'][0]['basis'],'hypothesis')
        plan['edits'][0]['references']=['condition:unqueried']
        self.validate_grounded(plan)

    def test_planner_sees_full_archive_and_exact_selected_strategy(self):
        c=dict(self.context(),revision_memory=self.memory(),strategy_document='SELECTED_STRATEGY')
        prompts=[]
        r=run_planner(c,lambda p,s:(prompts.append(p) or self.grounded_plan()),
                      lambda *a:self.fail('Replay queried'),queries_remaining=0,
                      acquisition='replay',max_actions=1)
        for text in ('SELECTED_STRATEGY','EARLY_FINAL','EARLY_ANALYSIS','EARLY_PAYLOAD','old_trigger'):
            self.assertIn(text,prompts[0])
        self.assertIn('library supplies methods and prior hypotheses',prompts[0])
        self.assertEqual(r['plan']['cause'],'unknown')

    def test_replay_allows_one_recorded_correction_without_query(self):
        c=dict(self.context(),revision_memory=self.memory(),strategy_document='SELECTED_STRATEGY')
        prompts=[];saved=[]
        def decide(prompt,stage):
            prompts.append(prompt)
            return 'not JSON' if len(prompts)==1 else self.grounded_plan()
        r=run_planner(c,decide,lambda *a:self.fail('Replay queried'),queries_remaining=0,
                      acquisition='replay',max_actions=1,persist=saved.append)
        self.assertEqual(len(prompts),2)
        self.assertIn('"previous_actions": [{"error":',prompts[1])
        self.assertEqual(r['format_corrections'],1)
        self.assertEqual(r['queries'],[])
        self.assertEqual(r['queries_remaining'],0)
        self.assertEqual(saved[-1]['plan']['cause'],'unknown')
        prompts.clear();saved.clear()
        with self.assertRaisesRegex(ValueError,'valid final plan'):
            run_planner(c,lambda p,s:(prompts.append(p) or 'not JSON'),None,
                        queries_remaining=0,acquisition='replay',max_actions=1,persist=saved.append)
        self.assertEqual(len(prompts),2)
        self.assertIsNone(saved[-1]['plan'])
        self.assertEqual(sum('error' in item for item in saved[-1]['actions']),2)

    def test_sampling_capability_survives_format_correction_without_becoming_evidence(self):
        context = dict(self.context(), revision_memory=self.memory(),
                       strategy_document='SELECTED_STRATEGY')
        original = deepcopy(context)
        block = 'AUTHORIZED EVIDENCE-RETURN CHANNEL: receiver@example.com; nonce=0123456789abcdef'
        prompts = []

        def decide(prompt, stage):
            prompts.append(prompt)
            return 'not JSON' if len(prompts) == 1 else self.grounded_plan()

        result = run_planner(context, decide, lambda *a: self.fail('Replay queried'),
                             queries_remaining=0, acquisition='replay', max_actions=1,
                             sampling_block=block)
        self.assertEqual(len(prompts), 2)
        for prompt in prompts:
            self.assertIn(block, prompt)
            data = json.loads(prompt.split('PUBLIC INPUT:\n', 1)[1])
            self.assertFalse(data['allow_query'])
            self.assertEqual(data['evidence'], {})
            self.assertNotIn('0123456789abcdef', json.dumps(data))
        self.assertEqual(context, original)
        self.assertEqual(result['queries'], [])
        self.assertEqual(result['evidence'], {})

    def test_grounded_transport_value_error_is_not_a_schema_correction(self):
        c=dict(self.context(),revision_memory=self.memory(),strategy_document='SELECTED_STRATEGY')
        calls=[]
        def decide(prompt,stage):
            calls.append(prompt)
            raise ValueError('Transport did not return a plan')
        with self.assertRaisesRegex(ValueError,'Transport did not return a plan'):
            run_planner(c,decide,None,queries_remaining=0,acquisition='replay',max_actions=1)
        self.assertEqual(len(calls),1)

    def test_generator_uses_full_projected_memory_and_fixed_strategy(self):
        c=dict(self.context(),revision_memory=self.memory(),strategy_document='SELECTED_STRATEGY',
               suite_name='fixture',user_task_id='u',injection_task_id='i',
               context_with_placeholder='{INJECTION:x}',injection_candidates=['x'])
        sample=dict(history=[dict(iter=1,injection='P0',final_output='LATEST_FINAL',
                                  trajectory='PRIVATE_TRAJECTORY')])
        prompt=generation_prompt(sample,'SELECTED_STRATEGY',c,{},self.grounded_plan(),[],'grounded')
        for text in ('EARLY_FINAL','EARLY_ANALYSIS','EARLY_PAYLOAD','positive_reference_candidates'):
            self.assertIn(text,prompt)
        self.assertEqual(prompt.count('SELECTED_STRATEGY'),1)
        self.assertNotIn('PRIVATE_TRAJECTORY',prompt)
        self.assertNotIn('Infer the failure mode',prompt)
        with self.assertRaises(ValueError):
            generation_prompt(sample,'A_DIFFERENT_STRATEGY',c,{},self.grounded_plan(),[],'grounded')

    def test_all_historical_report_suffixes_are_only_cleaned_in_model_view(self):
        memory=self.memory()
        memory['attempts'][0]['final_output']='EARLY_FINAL\n(no tool call)'
        memory['attempts'][1]['final_output']='They quoted (no tool call) in a document.'
        memory['facts'][-1]['value']='REPORTED_DONE\n(no tool call)'
        c=dict(self.context(),revision_memory=memory,strategy_document='SELECTED_STRATEGY',
               suite_name='fixture',user_task_id='u',injection_task_id='i',
               context_with_placeholder='{INJECTION:x}',injection_candidates=['x'])
        view=feedback_view(c)
        self.assertEqual(view['revision_memory']['attempts'][0]['final_output'],'EARLY_FINAL')
        self.assertEqual(view['revision_memory']['facts'][-1]['value'],'REPORTED_DONE')
        self.assertEqual(view['revision_memory']['attempts'][1]['final_output'],
                         'They quoted (no tool call) in a document.')
        self.assertTrue(c['revision_memory']['attempts'][0]['final_output'].endswith('\n(no tool call)'))
        self.assertTrue(c['revision_memory']['facts'][-1]['value'].endswith('\n(no tool call)'))
        prompts=[]
        run_planner(c,lambda p,s:(prompts.append(p) or self.grounded_plan()),None,
                    queries_remaining=0,acquisition='replay',max_actions=1)
        prompt=generation_prompt(dict(history=[]),'SELECTED_STRATEGY',c,{},self.grounded_plan(),[],'grounded')
        for rendered in (prompts[0],prompt):
            self.assertNotIn('EARLY_FINAL\\n(no tool call)',rendered)
            self.assertNotIn('REPORTED_DONE\\n(no tool call)',rendered)

if __name__=='__main__':unittest.main()
