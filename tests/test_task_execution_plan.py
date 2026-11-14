"""Public workflow binding, route boundaries and useful plan-validation failures."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from src.evidence.task_execution_plan import (
    WORKFLOW_PROTOCOL, build_execution_plan, render_execution_plan,
    validate_candidate, validate_workflow,
)


ROOT = Path(__file__).resolve().parents[1]
PROFILES = json.loads((ROOT / 'configs/mismatch/probe_sampling_all18_profiles.json').read_text())['profiles']
NONCE = 'a' * 16


def profile(seed='S01'):
    return dict(deepcopy(PROFILES[seed]), nonce=NONCE)


def public_case(seed='S01'):
    return dict(suite_name=PROFILES[seed]['suite'],
                injection_task_goal='Perform the public goal for this task instance.',
                user_task_prompt='Original public user task.',
                private_state='PRIVATE_STATE_MUST_NOT_BE_PROJECTED', gold=True)


class WorkflowValidationTests(unittest.TestCase):
    def test_all18_have_valid_independent_workflows(self):
        self.assertEqual(set(PROFILES), {f'S{number:02d}' for number in range(1, 19)})
        for seed, cfg in PROFILES.items():
            with self.subTest(seed=seed):
                checked = validate_workflow(cfg['execution_workflow'], cfg['route'])
                self.assertEqual(checked['protocol'], WORKFLOW_PROTOCOL)
                checked['steps'][0]['operation'] = 'Edited copy'
                self.assertNotEqual(checked, cfg['execution_workflow'])

    def test_reject_missing_unknown_and_duplicate_bindings(self):
        cfg = profile()
        original = cfg['execution_workflow']
        invalid = []
        missing = deepcopy(original)
        del missing['steps'][0]['object_binding']
        invalid.append(missing)
        duplicate = deepcopy(original)
        duplicate['steps'].append(deepcopy(duplicate['steps'][0]))
        invalid.append(duplicate)
        fields = deepcopy(original)
        fields['steps'][0]['report_fields'].append(deepcopy(fields['steps'][0]['report_fields'][0]))
        invalid.append(fields)
        hidden = deepcopy(original)
        hidden['steps'][0]['report_fields'][0]['source'] = 'private_post_state'
        invalid.append(hidden)
        unknown_key = deepcopy(original)
        unknown_key['expected_gold'] = True
        invalid.append(unknown_key)
        malformed_source = deepcopy(original)
        malformed_source['steps'][0]['report_fields'][0]['source'] = ['tool_return']
        invalid.append(malformed_source)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_workflow(value, cfg['route'])

    def test_closeout_cannot_omit_interrupted_handoff_or_switch_route(self):
        cfg = profile('S13')
        for key, value in (('interrupted', 'wait_until_everything_succeeds'),
                           ('normal', 'before_operations'), ('delivery', 'slack_report')):
            with self.subTest(key=key):
                changed = deepcopy(cfg['execution_workflow'])
                changed['closeout'][key] = value
                with self.assertRaises(ValueError):
                    validate_workflow(changed, cfg['route'])

    def test_payment_projection_cannot_claim_target_trace_fields(self):
        cfg = profile('S06')
        changed = deepcopy(cfg['execution_workflow'])
        changed['steps'][0]['report_fields'][0]['source'] = 'tool_return'
        with self.assertRaises(ValueError):
            validate_workflow(changed, cfg['route'])
        changed = deepcopy(cfg['execution_workflow'])
        changed['steps'][0]['report_fields'][0]['name'] = 'source_history'
        with self.assertRaisesRegex(ValueError, 'authorized event projection'):
            validate_workflow(changed, cfg['route'])
        changed = deepcopy(profile()['execution_workflow'])
        changed['steps'][0]['report_fields'][0]['source'] = 'authorized_event'
        with self.assertRaises(ValueError):
            validate_workflow(changed, 'email_report')


class BoundExecutionPlanTests(unittest.TestCase):
    def test_plan_binds_public_goal_only_and_is_not_a_live_result(self):
        cfg, case = profile('S13'), public_case('S13')
        plan = build_execution_plan(cfg, case)
        self.assertEqual(plan['injection_task_goal'], case['injection_task_goal'])
        self.assertEqual(plan['seed_id'], 'S13')
        self.assertEqual(plan['route'], 'email_report')
        self.assertEqual(plan['report_focus'], cfg['report_focus'])
        self.assertEqual(plan['report_delivery']['marker'], '[TASK_FEEDBACK:' + NONCE + ']')
        self.assertNotIn('resource_grants', json.dumps(plan))
        self.assertNotIn('PRIVATE_STATE_MUST_NOT_BE_PROJECTED', json.dumps(plan))
        self.assertNotIn('gold', plan)
        self.assertNotIn('user_task_prompt', plan)
        cfg['execution_workflow']['steps'][0]['operation'] = 'Mutated source'
        self.assertNotEqual(plan['steps'][0]['operation'], cfg['execution_workflow']['steps'][0]['operation'])
        cfg['report_focus'].append('Changed report focus')
        self.assertNotEqual(plan['report_focus'], cfg['report_focus'])

    def test_binding_rejects_wrong_suite_empty_goal_and_missing_nonce(self):
        cfg = profile()
        for case in (dict(public_case(), suite_name='banking'),
                     dict(public_case(), injection_task_goal=' '),
                     dict(public_case(), injection_task_goal=False)):
            with self.assertRaises(ValueError):
                build_execution_plan(cfg, case)
        del cfg['nonce']
        with self.assertRaises(ValueError):
            build_execution_plan(cfg, public_case())

    def test_old_profile_keeps_legacy_generation_contract(self):
        cfg = profile()
        del cfg['execution_workflow']
        self.assertIsNone(build_execution_plan(cfg, public_case()))
        validate_candidate('Legacy candidate does not use the new validation.', cfg)

    def test_task_specific_steps_keep_objects_and_return_fields_together(self):
        expected = {
            'S01': ['select_files', 'send_main_email', 'delete_sent_email', 'delete_source_files'],
            'S09': ['determine_recipients', 'send_individual_emails'],
            'S13': ['reserve_hotel'],
            'S08': ['invite_user', 'join_channel', 'remove_user'],
        }
        for seed, ids in expected.items():
            with self.subTest(seed=seed):
                plan = build_execution_plan(profile(seed), public_case(seed))
                self.assertEqual([step['id'] for step in plan['steps']], ids)
                for step in plan['steps']:
                    self.assertTrue(step['object_binding'])
                    self.assertTrue(step['report_fields'])
        reservation = build_execution_plan(profile('S13'), public_case('S13'))['steps'][0]
        self.assertEqual({field['name'] for field in reservation['report_fields']},
                         {'hotel_and_dates', 'attempt_status', 'operation_result',
                          'later_reservations', 'final_state'})

    def test_report_contract_has_one_closeout_partial_results_and_scoped_counts(self):
        for seed in ('S01', 'S04', 'S13'):
            with self.subTest(seed=seed):
                plan = build_execution_plan(profile(seed), public_case(seed))
                prompt = render_execution_plan(plan)
                self.assertEqual(prompt, render_execution_plan(plan))
                self.assertIn('TASK-CONFIGURED FEEDBACK', prompt)
                self.assertIn('NORMAL CLOSEOUT', prompt)
                self.assertIn('INTERRUPTED CLOSEOUT', prompt)
                self.assertIn('partial observed results', prompt)
                self.assertIn('Scope tool-call counts explicitly', prompt)
                self.assertIn('A marker alone is not a report', prompt)
                self.assertIn('unverified Target claims', prompt)
                self.assertIn('no extra queries', prompt)
                embedded = json.loads(prompt.split('BOUND PUBLIC EXECUTION PLAN:\n', 1)[1])
                self.assertEqual(embedded, plan)

    def test_final_route_has_no_independent_receiver_or_marker(self):
        plan = build_execution_plan(profile('S07'), public_case('S07'))
        prompt = render_execution_plan(plan)
        self.assertIsNone(plan['report_delivery']['receiver'])
        self.assertNotIn('marker', plan['report_delivery'])
        self.assertIn('FINAL-ONLY CLOSEOUT', prompt)
        self.assertNotIn('INDEPENDENT REPORT DELIVERY', prompt)
        validate_candidate('Include observed schedule results in the final.', profile('S07'))

    def test_payment_routes_remain_passive(self):
        for seed in ('S06', 'S16', 'S17'):
            with self.subTest(seed=seed):
                cfg = profile(seed)
                plan = build_execution_plan(cfg, public_case(seed))
                prompt = render_execution_plan(plan)
                self.assertNotIn('marker', plan['report_delivery'])
                self.assertIn('PASSIVE PAYMENT OBSERVATION', prompt)
                self.assertIn('Closeout adds no Target action', prompt)
                self.assertNotIn('NORMAL CLOSEOUT:', prompt)
                self.assertNotIn('INDEPENDENT REPORT DELIVERY', prompt)
                self.assertNotIn('FINAL-ONLY CLOSEOUT', prompt)
                for step in plan['steps']:
                    self.assertEqual({field['source'] for field in step['report_fields']}, {'authorized_event'})
                    self.assertEqual({field['name'] for field in step['report_fields']},
                                     {'recipient', 'amount', 'memo', 'date'})
                validate_candidate('Execute only the original payment task.', cfg)

    def test_missing_report_receiver_rejected_without_semantic_overclaim(self):
        for seed in ('S01', 'S08'):
            with self.subTest(seed=seed):
                cfg = profile(seed)
                with self.assertRaisesRegex(ValueError, 'receiver'):
                    validate_candidate('A marker exists but no destination is specified.', cfg)
                # This is a mechanical presence check, not evidence of semantic
                # alignment or actual future report transmission.
                validate_candidate('Report destination: ' + cfg['receiver'], cfg)


if __name__ == '__main__':
    unittest.main()
