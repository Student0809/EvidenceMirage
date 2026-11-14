"""Shared task plans survive revision projection and existing advisory review."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from src.evidence.task_execution_plan import build_execution_plan, render_execution_plan
from src.vendor.refinement import generation_prompt
from src.vendor.revision_checker import review_semantics, SEMANTIC_DIMENSIONS
from src.vendor.revision_planner import feedback_view, project_context, run_planner


ROOT = Path(__file__).resolve().parents[1]


def fixture(seed='S01'):
    profiles = json.loads((ROOT / 'configs/mismatch/probe_sampling_all18_profiles.json').read_text())['profiles']
    profile = dict(profiles[seed], nonce='a' * 16)
    sample = dict(suite_name=profile['suite'], user_task_id='user_fixture',
                  injection_task_id='injection_fixture', user_task_prompt='Original task.',
                  injection_task_goal='Perform the fixture operations on the public task objects.',
                  context_with_placeholder='Public task input {INJECTION:fixture}',
                  injection_candidates=['fixture'], history=[dict(iter=0, injection='P0',
                      security=False, utility=None, final_output='Current outcome unknown.',
                      oracle_run_id='fixture/1')])
    context = project_context(sample, {})
    context['task_execution_plan'] = build_execution_plan(profile, sample)
    return sample, context


def revision_plan(goal):
    return dict(action='make_plan', observations=[], hypothesis='The closeout may have been omitted.',
                edit_scope='Bind existing operation results to closeout.', preserve=[],
                unknowns=['The reason for the missing report is unknown.'], objective=goal,
                edits=[dict(change='Clarify closeout for the existing operations.', basis='hypothesis',
                            references=['final_report'], expected_effect='A report with observed results.')])


class SharedRevisionPlanTests(unittest.TestCase):
    def test_all_routes_use_the_same_plan_in_planner_and_generator(self):
        for seed in ('S01', 'S08', 'S06', 'S07'):
            with self.subTest(seed=seed):
                sample, context = fixture(seed)
                shared = deepcopy(context['task_execution_plan'])
                rendered = render_execution_plan(shared)
                sampling_block = 'TASK-CONFIGURED FEEDBACK\n' + rendered
                calls = []

                def decide(prompt, stage):
                    calls.append((stage, prompt))
                    return revision_plan(sample['injection_task_goal'])

                record = run_planner(context, decide, None, queries_remaining=0,
                                     acquisition='replay', sampling_block=sampling_block)
                self.assertEqual([stage for stage, _ in calls], ['planner'])
                public = json.loads(calls[0][1].split('PUBLIC INPUT:\n', 1)[1])
                self.assertEqual(public['context']['task_execution_plan'], shared)
                self.assertEqual(record['task_execution_plan'], shared)
                self.assertEqual(record['queries'], [])
                self.assertNotIn('Do not prescribe a fixed report template', calls[0][1])
                generated_prompt = generation_prompt(sample, 'STRATEGY', context, {},
                    record['plan'], [], 'planned', sampling_block=sampling_block)
                self.assertEqual(generated_prompt.count(rendered), 1)
                self.assertEqual(calls[0][1].count(rendered), 1)
                self.assertEqual(context['task_execution_plan'], shared)

    def test_direct_generator_still_receives_the_exact_shared_plan(self):
        sample, context = fixture('S13')
        rendered = render_execution_plan(context['task_execution_plan'])
        prompt = generation_prompt(sample, 'STRATEGY', context, {},
            revision_plan(sample['injection_task_goal']), [], 'planned')
        self.assertEqual(prompt.count(rendered), 1)

    def test_feedback_projection_copies_plan_without_creating_observations(self):
        _, context = fixture()
        context['final_response'] = 'Reported done\n(no tool call)'
        projected = feedback_view(context)
        self.assertEqual(projected['task_execution_plan'], context['task_execution_plan'])
        self.assertEqual(projected['final_response'], 'Reported done')
        projected['task_execution_plan']['injection_task_goal'] = 'Changed copy'
        self.assertNotEqual(projected['task_execution_plan'], context['task_execution_plan'])
        self.assertEqual(context['final_response'], 'Reported done\n(no tool call)')

    def test_legacy_contract_and_no_probe_do_not_acquire_a_workflow(self):
        sample, context = fixture()
        context.pop('task_execution_plan')
        prompts = []
        record = run_planner(context, lambda prompt, stage: (
            prompts.append(prompt) or revision_plan(sample['injection_task_goal'])),
            None, queries_remaining=0, sampling_block='LEGACY SAMPLING CONTRACT')
        self.assertIn('Do not prescribe a fixed report template', prompts[0])
        self.assertNotIn('task_execution_plan', record)
        self.assertNotIn('task_execution_plan', json.loads(prompts[0].split('PUBLIC INPUT:\n', 1)[1])['context'])
        _, fused = fixture()
        with self.assertRaisesRegex(ValueError, 'no_probe planning cannot carry a probe contract'):
            run_planner(fused, lambda *args: self.fail('No model call expected'), None,
                        queries_remaining=0, no_probe=True)

    def test_advisory_review_can_anchor_a_workflow_conflict(self):
        _, context = fixture('S08')
        candidate = 'Send only the marker.'
        workflow_quote = json.dumps(context['task_execution_plan']['route'])
        audit = dict(assessments={name: ('conflict' if name == 'plan_following' else 'unknown')
                                 for name in SEMANTIC_DIMENSIONS},
                     findings=[dict(dimension='plan_following', explanation='The report has no results.',
                         quotes=[dict(source='candidate', text=candidate),
                                 dict(source='task_execution_plan', text=workflow_quote)])])
        calls = []
        result = review_semantics('P0', candidate, {}, context, {},
            lambda prompt, stage: (calls.append((stage, prompt)) or audit))
        self.assertEqual([stage for stage, _ in calls], ['semantic_review'])
        sources = json.loads(calls[0][1].split('SOURCES:\n', 1)[1])
        self.assertEqual(json.loads(sources['task_execution_plan']), context['task_execution_plan'])
        self.assertEqual(result['enforcement'], 'advisory')
        self.assertFalse(result['interpretation_verified'])
        forged = deepcopy(audit)
        forged['findings'][0]['quotes'][1]['text'] = 'not present in the shared workflow'
        with self.assertRaisesRegex(ValueError, 'quote not present'):
            review_semantics('P0', candidate, {}, context, {}, lambda *args: forged)


if __name__ == '__main__':
    unittest.main()
