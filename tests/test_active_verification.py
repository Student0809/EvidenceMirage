"""Offline native-tool checks; the scripted LLM never makes network requests."""
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agentdojo.default_suites
from agentdojo.functions_runtime import FunctionCall
from agentdojo.task_suite.load_suites import get_suite

from src.adapters.agentdojo import AgentDojoAdapter
from src.engine.config import load_cases, load_config
from src.engine.storage import plain
from src.evidence.active_verification import (
    PROTOCOL, VerificationRuntime, public_packets, run_followup, validate_config,
)
from src.feedback.schema import public_packets as validate_packets
from src.mismatch.online import live_packets
from src.schema import Payload


def assistant(text, calls=()):
    return dict(role='assistant', content=[dict(type='text', content=text)],
                tool_calls=[FunctionCall(function=name, args=args, id=f'call_{i}')
                            for i, (name, args) in enumerate(calls)])


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def query(self, prompt, runtime, env, messages):
        self.requests.append(deepcopy(messages))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return prompt, runtime, env, [*messages, reply], {}


class ActiveVerificationTests(unittest.TestCase):
    def setUp(self):
        self.config = validate_config(dict(protocol=PROTOCOL, mode='followup', email_ids=['34'],
                                          file_ids=['11', '1', '3', '4', '12']))
        self.case = load_cases(load_config('experiments/e0_smoke.yaml'))[5]
        self.suite = get_suite('v1.2.2', 'workspace')
        self.env = self.suite.load_and_inject_default_environment({})

    def reads(self):
        return ScriptedLLM([assistant('', [('get_sent_emails', {}), ('list_files', {})]),
                            assistant('Verified current membership.')])

    def test_actual_reads_show_partial_deletion_and_trash_limit(self):
        runtime = VerificationRuntime(self.suite.tools, self.config, readonly=False)
        mail, error = runtime.run_function(self.env, 'send_email', dict(
            recipients=['mark.black-2134@gmail.com'], subject='Backup', body='Test'))
        self.assertIsNone(error)
        self.config['email_ids'] = [mail.id_]
        runtime.run_function(self.env, 'delete_email', dict(email_id=mail.id_))
        runtime.run_function(self.env, 'delete_file', dict(file_id='11'))
        before = plain(self.env)
        report = run_followup(self.reads(), self.suite.tools, self.env,
                              [assistant('All deleted.')], self.config)
        self.assertEqual(report['status'], 'completed')
        states = {(f['kind'], f['object_id']): f['state'] for f in report['facts']}
        self.assertEqual(states['email', mail.id_], 'absent')
        self.assertEqual(states['file', '11'], 'absent')
        self.assertEqual(states['file', '12'], 'present')
        self.assertIn(mail.id_, self.env.inbox.trash)
        self.assertEqual(report['facts'][0]['scope'], 'active_sent_mail')
        self.assertEqual(report['tool_queries'], 2)
        self.assertEqual(plain(self.env), before)
        packets = validate_packets(public_packets(report))
        self.assertEqual(packets, [])
        self.assertEqual(report['visibility'], 'evaluator_only')
        self.assertFalse(report['jailbreak_sampling'])

    def test_self_report_and_tool_errors_remain_unknown(self):
        for llm in (ScriptedLLM([assistant('I checked: everything is deleted.')]),
                    ScriptedLLM([assistant('', [('list_files', {'bogus': True})]),
                                 assistant('All gone.')]),
                    ScriptedLLM([RuntimeError('provider unavailable')])):
            with self.subTest(llm=llm):
                report = run_followup(llm, self.suite.tools, self.env, [], self.config)
                self.assertTrue(all(f['state'] == 'unknown' for f in report['facts']))
                self.assertEqual(public_packets(report), [])

    def test_write_attempt_is_blocked_and_query_budget_is_enforced(self):
        before = plain(self.env)
        config = dict(self.config, max_queries=1)
        llm = ScriptedLLM([assistant('', [('delete_file', {'file_id': '12'}),
                                          ('get_sent_emails', {}), ('list_files', {})]),
                           assistant('Done.')])
        report = run_followup(llm, self.suite.tools, self.env, [], config)
        self.assertEqual(plain(self.env), before)
        self.assertEqual(report['tool_queries'], 1)
        self.assertTrue(all(f['state'] == 'unknown' for f in report['facts'] if f['kind'] == 'file'))
        errors = [m['error'] for m in report['messages'] if m['role'] == 'tool']
        self.assertIn('Invalid tool delete_file provided.', errors)
        self.assertIn('Verification query budget exhausted', errors)

    def test_read_before_subsequent_write_is_not_final_state_evidence(self):
        runtime = VerificationRuntime(self.suite.tools, self.config, readonly=False)
        runtime.run_function(self.env, 'list_files', {})
        runtime.run_function(self.env, 'delete_file', {'file_id': '11'})
        self.assertTrue(all(f['state'] == 'unknown' for f in runtime.report()['facts']))
        runtime.run_function(self.env, 'list_files', {})
        self.assertEqual(runtime.report()['facts'][1]['state'], 'absent')

    def test_adapter_preserves_original_result_and_projects_live_packets(self):
        seen = []
        def query(prompt, runtime, env):
            seen.append(prompt)
            return prompt, runtime, env, [assistant('Original response.')], {}
        pipeline = SimpleNamespace(query=query)
        baseline = AgentDojoAdapter(pipeline=pipeline).execute(self.case, Payload('p', 'x'), 'baseline')
        adapter = AgentDojoAdapter(pipeline=pipeline, active_verification=self.config)
        adapter.llm = self.reads()
        result = adapter.execute(self.case, Payload('p', 'x'), 'active')
        self.assertIsNone(result.error)
        self.assertEqual((result.gold, result.utility, result.final),
                         (baseline.gold, baseline.utility, baseline.final))
        self.assertEqual(result.raw['post'], baseline.raw['post'])
        self.assertEqual(seen, [self.case['user_task_prompt']] * 2)
        self.assertEqual(result.raw['active_verification']['final'], 'Verified current membership.')
        self.assertEqual(validate_packets(live_packets(asdict(result), 'workspace')), [])
        adapter.llm = ScriptedLLM([RuntimeError('outage')])
        failed = adapter.execute(self.case, Payload('p', 'x'), 'failed')
        self.assertEqual(failed.gold, baseline.gold)
        self.assertIsNone(failed.error)
        self.assertEqual(failed.raw['active_verification']['status'], 'error')

    def test_embedded_mode_changes_prompt_and_captures_real_reads(self):
        seen = []
        def query(prompt, runtime, env):
            seen.append(prompt)
            runtime.run_function(env, 'get_sent_emails', {})
            runtime.run_function(env, 'list_files', {})
            return prompt, runtime, env, [assistant('Checked.')], {}
        adapter = AgentDojoAdapter(pipeline=SimpleNamespace(query=query),
                                  active_verification=dict(self.config, mode='embedded'))
        result = adapter.execute(self.case, Payload('p', 'x'), 'embedded')
        self.assertIsNone(result.error)
        self.assertIn('After completing the task', seen[0])
        self.assertTrue(all(f['state'] != 'unknown' for f in result.raw['active_verification']['facts']))

    def test_mutating_read_implementation_fails_closed(self):
        tools = [f.model_copy(update={'run': lambda cloud_drive: cloud_drive.files.pop('11')})
                 if f.name == 'list_files' else f for f in self.suite.tools]
        before = plain(self.env)
        report = run_followup(self.reads(), tools, self.env, [], self.config)
        self.assertEqual(report['error'], 'VerificationStateMutation')
        self.assertTrue(all(f['state'] == 'unknown' for f in report['facts']))
        self.assertEqual(plain(self.env), before)

    def test_config_requires_explicit_ids_and_valid_budgets(self):
        for config in ({}, dict(mode='followup'), dict(self.config, max_rounds=True),
                       dict(self.config, file_ids=['1', '1']), dict(self.config, mode='oracle')):
            with self.assertRaises(ValueError):
                validate_config(config)

    def test_round_budget_exhaustion_retains_tool_evidence_without_fake_final(self):
        report = run_followup(self.reads(), self.suite.tools, self.env, [],
                              dict(self.config, max_rounds=1))
        self.assertEqual(report['status'], 'round_budget_exhausted')
        self.assertIsNone(report['final'])
        self.assertTrue(all(f['state'] != 'unknown' for f in report['facts']))

    def test_provider_accounting_separates_extra_requests_and_keeps_original_final(self):
        from openai.types.chat import ChatCompletion
        responses = []
        for content, calls in [('Original.', None), (None, [
                dict(id='mail', type='function', function=dict(name='get_sent_emails', arguments='{}')),
                dict(id='files', type='function', function=dict(name='list_files', arguments='{}'))]),
                ('Verification report.', None)]:
            responses.append(ChatCompletion.model_validate(dict(id='offline', created=0,
                model='gpt-5-nano', object='chat.completion', choices=[dict(index=0,
                    finish_reason='tool_calls' if calls else 'stop',
                    message=dict(role='assistant', content=content, tool_calls=calls))],
                usage=dict(prompt_tokens=10, completion_tokens=2, total_tokens=12))))
        replies = iter(responses)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: next(replies))))
        with patch('openai.OpenAI', return_value=client):
            adapter = AgentDojoAdapter(active_verification=self.config)
        result = adapter.execute(self.case, Payload('p', 'x'), 'accounting')
        self.assertIsNone(result.error)
        self.assertEqual(result.final, 'Original.')
        self.assertEqual(result.usage['api_calls'], 3)
        self.assertEqual(result.usage['input_tokens'], 30)
        self.assertEqual([c['stage'] for c in result.raw['api_calls']],
                         ['target_http', 'active_verification_http', 'active_verification_http'])
        self.assertEqual(result.raw['active_verification']['api_calls'], 2)
        self.assertEqual(result.raw['active_verification']['tool_queries'], 2)

    def test_active_packets_obey_existing_feedback_view_ablation(self):
        from dataclasses import replace
        from src.mismatch.data import ROOT, load_attempt
        from src.mismatch.online import selection
        _, instances = selection(ROOT / 'data/mismatch_autonomous_20261104')
        attempt = load_attempt(instances[0][0])
        report = run_followup(self.reads(), self.suite.tools, self.env, [], self.config)
        attempt = replace(attempt, packets=tuple(public_packets(report)), origin='target_execution')
        self.assertEqual(attempt.view('final_only')['packets'], [])
        self.assertEqual(attempt.view('authorized_evidence')['packets'], [])


if __name__ == '__main__':
    unittest.main()
