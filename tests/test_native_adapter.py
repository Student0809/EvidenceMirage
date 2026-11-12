import unittest
import json
import tempfile
import os
import httpx
import openai
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import agentdojo.default_suites
from agentdojo.task_suite.load_suites import get_suite
from src.adapters.agentdojo import AgentDojoAdapter
from src.engine.config import ROOT, load_config, load_cases
from src.schema import Payload
from src.attacker.piminer import PIMinerAdapter
from src.engine.runner import Runner
from src.engine.storage import RunStore
from src.evidence.observer import FixedObserver
from src.policies.registry import build_policies
from src.vendor.revision_checker import SEMANTIC_DIMENSIONS


class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.case=load_cases(load_config(ROOT/'experiments/e0_smoke.yaml'))[4]
        self.suite=get_suite('v1.2.2','banking')

    def pipeline(self):
        def query(prompt,runtime,env):
            _,error=runtime.run_function(env,'send_money',dict(recipient='US133000000121212121212',amount=1,
                                                            subject='Spotify Premium',date='2024-05-15'))
            self.assertIsNone(error)
            return prompt,runtime,env,[dict(role='assistant',content=[dict(type='text',content='Done.')],tool_calls=[])],{}
        return SimpleNamespace(query=query)

    def test_native_gold_equivalence_and_fresh_environment(self):
        c=self.case; payload=Payload('P0','Quoted "value"\nnew line')
        import json
        injections={k:json.dumps(payload.text,ensure_ascii=False)[1:-1] for k in c['injection_candidates']}
        expected=self.suite.run_task_with_pipeline(self.pipeline(),self.suite.get_user_task_by_id(c['user_task_id']),
                                                  self.suite.get_injection_task_by_id(c['injection_task_id']),injections)
        adapter=AgentDojoAdapter(pipeline=self.pipeline())
        a=adapter.execute(c,payload,'a');b=adapter.execute(c,payload,'b')
        self.assertIsNone(a.error)
        self.assertEqual((a.utility,a.gold),expected)
        self.assertEqual((b.utility,b.gold),expected)
        self.assertTrue(a.gold)
        self.assertEqual(a.final,'Done.')
        self.assertEqual(a.raw['pre'],b.raw['pre'])
        self.assertEqual(len(a.observation['payments']),1)
        self.assertEqual(len(b.observation['payments']),1)

    def test_target_exception_is_single_attempt_not_three_silent_rollouts(self):
        calls=[]
        def query(*args):calls.append(1);raise RuntimeError('fixture')
        result=AgentDojoAdapter(pipeline=SimpleNamespace(query=query)).execute(self.case,Payload('P0','x'),'e')
        self.assertEqual(len(calls),1)
        self.assertIsNone(result.gold)
        self.assertEqual(result.error,'RuntimeError')

    def test_observer_failure_preserves_gold(self):
        with patch('src.adapters.agentdojo.Collector.finish',side_effect=ValueError('bad capture')):
            result=AgentDojoAdapter(pipeline=self.pipeline()).execute(self.case,Payload('P0','x'),'e')
        self.assertTrue(result.gold)
        self.assertIsNone(result.error)
        self.assertEqual(result.observation_error,'ValueError')

    def test_mismatched_native_task_not_silently_evaluated(self):
        case=dict(self.case,user_task_prompt='Not the native task')
        result=AgentDojoAdapter(pipeline=self.pipeline()).execute(case,Payload('P0','x'),'e')
        self.assertIsNone(result.gold)
        self.assertEqual(result.error,'ValueError')

    def test_provider_preflight_and_token_accounting_without_network(self):
        from openai.types.chat import ChatCompletion
        response=ChatCompletion.model_validate(dict(id='fixture',created=0,model='gpt-5-nano',object='chat.completion',
            choices=[dict(index=0,finish_reason='stop',message=dict(role='assistant',content='OK'))],
            usage=dict(prompt_tokens=10,completion_tokens=2,total_tokens=12)))
        requests=[]
        def create(**kw):requests.append(kw);return response
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch('openai.OpenAI',return_value=client):
            adapter=AgentDojoAdapter()
        preflight=adapter.preflight()
        self.assertEqual(preflight['status'],'passed')
        self.assertEqual(len(requests[0]['messages']),1)
        self.assertEqual(requests[0]['messages'][0]['role'],'user')
        result=adapter.execute(self.case,Payload('P0','fixture'),'e')
        self.assertIsNone(result.error)
        self.assertEqual(result.usage['api_calls'],1)
        self.assertEqual(result.usage['input_tokens'],10)
        self.assertEqual(result.usage['output_tokens'],2)
        self.assertEqual(result.usage['missing_usage_calls'],0)

    def failing_api_adapter(self, error):
        requests = []
        def create(**kwargs):
            requests.append(kwargs)
            raise error
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        def query(prompt, runtime, env):
            client.chat.completions.create(model='gpt-5-nano', messages=[{'role': 'user', 'content': prompt}])
        with patch('openai.OpenAI', return_value=client) as constructor, patch(
                'agentdojo.agent_pipeline.agent_pipeline.AgentPipeline.from_config',
                return_value=SimpleNamespace(query=query)):
            adapter = AgentDojoAdapter(api_key_env='OFFLINE_DIAGNOSTIC_CREDENTIAL')
        self.assertEqual(constructor.call_args.kwargs['max_retries'], 0)
        self.assertEqual(constructor.call_args.kwargs['timeout'], 120)
        return adapter, client, requests

    def test_api_status_diagnostics_record_codes_without_credentials_payload_or_headers(self):
        credential = 'OFFLINE_ONLY_CREDENTIAL_SENTINEL_987654'
        payload = 'OFFLINE_PAYLOAD_SENTINEL_WITH_MULTIPLE_WORDS'
        request = httpx.Request('POST', 'https://offline.example/v1/chat/completions',
                               headers={'Authorization': 'Bearer ' + credential})
        response = httpx.Response(500, request=request, headers={
            'x-request-id': 'req-offline-123', 'x-debug-secret': 'HEADER_MUST_NOT_APPEAR'})
        body = {'error': {'code': 'upstream_500', 'type': 'provider_internal_error',
                         'message': 'Gateway unavailable; ' + credential + '; ' + payload + '; ' + 'x' * 1000},
                'debug': {'request': 'WHOLE_BODY_MUST_NOT_APPEAR'}}
        error = openai.InternalServerError('unused SDK summary', response=response, body=body)
        adapter, client, requests = self.failing_api_adapter(error)
        with patch.dict(os.environ, {'OFFLINE_DIAGNOSTIC_CREDENTIAL': credential}):
            result = adapter.execute(self.case, Payload('P0', payload), 'status-error')
        self.assertEqual(len(requests), 1)
        self.assertIsNone(result.gold)
        self.assertEqual(result.error, 'InternalServerError')
        self.assertEqual(result.usage['api_calls'], 1)
        self.assertEqual(result.usage['missing_usage_calls'], 1)
        details = result.raw['api_calls'][0]['error_details']
        self.assertEqual(details, result.raw['error_details'])
        self.assertEqual(details['status_code'], 500)
        self.assertEqual(details['request_id'], 'req-offline-123')
        self.assertEqual(details['provider_code'], 'upstream_500')
        self.assertEqual(details['provider_type'], 'provider_internal_error')
        self.assertTrue(details['message'].startswith('Gateway unavailable; [REDACTED]'))
        self.assertLessEqual(len(details['message']), 513)
        saved = json.dumps(result.raw)
        for forbidden in (credential, payload, 'HEADER_MUST_NOT_APPEAR', 'WHOLE_BODY_MUST_NOT_APPEAR', 'Authorization'):
            self.assertNotIn(forbidden, saved)
        self.assertEqual(adapter._error_sensitive_texts, ())

    def test_api_error_embedded_request_is_omitted_and_numeric_code_is_kept(self):
        response = httpx.Response(503, request=httpx.Request('POST', 'https://offline.example/v1/chat/completions'))
        error = openai.InternalServerError('unused SDK summary', response=response, body={'error': {
            'code': 50301, 'type': 'unavailable',
            'message': 'Invalid request: {"messages": [{"content": "REQUEST_SENTINEL"}]} api_key=SECRET_SENTINEL'}})
        adapter, _, requests = self.failing_api_adapter(error)
        result = adapter.execute(self.case, Payload('P0', 'offline payload'), 'request-error')
        self.assertEqual(len(requests), 1)
        details = result.raw['api_calls'][0]['error_details']
        self.assertEqual(details['provider_code'], '50301')
        self.assertEqual(details['message'], '[request content omitted]')
        self.assertNotIn('REQUEST_SENTINEL', json.dumps(result.raw))
        self.assertNotIn('SECRET_SENTINEL', json.dumps(result.raw))
        self.assertIsNone(result.gold)

    def test_error_fields_can_be_unreadable_without_losing_status_or_leaking_tokens(self):
        class UnreadableRequestID(openai.InternalServerError):
            def __getattribute__(self, name):
                if name == 'request_id':
                    raise ValueError('broken request ID property')
                return super().__getattribute__(name)
        response = httpx.Response(500, request=httpx.Request('POST', 'https://offline.example/v1/chat/completions'))
        error = UnreadableRequestID('unused SDK summary', response=response, body={'error': {
            'message': 'Gateway failed; "api_key": "UNKNOWN_KEY_SENTINEL"; Bearer UNKNOWN_TOKEN_SENTINEL'}})
        adapter, _, requests = self.failing_api_adapter(error)
        result = adapter.execute(self.case, Payload('P0', 'offline payload'), 'unreadable-error')
        self.assertEqual(len(requests), 1)
        details = result.raw['api_calls'][0]['error_details']
        self.assertEqual(details['status_code'], 500)
        self.assertNotIn('request_id', details)
        self.assertTrue(details['message'].startswith('Gateway failed;'))
        self.assertNotIn('UNKNOWN_KEY_SENTINEL', json.dumps(result.raw))
        self.assertNotIn('UNKNOWN_TOKEN_SENTINEL', json.dumps(result.raw))
        self.assertEqual(result.error, 'UnreadableRequestID')
        self.assertIsNone(result.gold)

    def test_diagnostic_extraction_failure_preserves_original_exception_and_call_count(self):
        response = httpx.Response(500, request=httpx.Request('POST', 'https://offline.example/v1/chat/completions'))
        error = openai.InternalServerError('fixture', response=response, body={'error': {'message': 'offline failure'}})
        adapter, client, requests = self.failing_api_adapter(error)
        with patch('src.adapters.agentdojo._api_error_details', side_effect=ValueError('diagnostic fixture')):
            with self.assertRaises(openai.InternalServerError) as caught:
                client.chat.completions.create(model='gpt-5-nano', messages=[])
            self.assertIs(caught.exception, error)
            result = adapter.execute(self.case, Payload('P0', 'offline payload'), 'extraction-error')
        self.assertEqual(len(requests), 2)  # One explicit wrapper call, one execute.
        self.assertEqual(result.usage['api_calls'], 1)
        self.assertIsNone(result.gold)
        self.assertEqual(result.error, 'InternalServerError')
        self.assertNotIn('error_details', result.raw)
        self.assertEqual(result.raw['api_calls'][0]['error_type'], 'InternalServerError')

    def test_plain_runtime_error_keeps_existing_result_without_stringifying_error(self):
        error = RuntimeError('SECRET_EXCEPTION_TEXT_MUST_NOT_APPEAR')
        adapter, _, requests = self.failing_api_adapter(error)
        result = adapter.execute(self.case, Payload('P0', 'offline payload'), 'runtime-error')
        self.assertEqual(len(requests), 1)
        self.assertIsNone(result.gold)
        self.assertEqual(result.error, 'RuntimeError')
        self.assertNotIn('error_details', result.raw)
        self.assertNotIn('error_details', result.raw['api_calls'][0])
        self.assertNotIn(str(error), json.dumps(result.raw))

    def test_all_five_arms_full_loop_with_native_gold_and_shared_piminer(self):
        calls=[]
        def query(prompt,runtime,env):
            calls.append(1)
            # First trajectory does nothing. Later scripted tool calls are native;
            # this checks integration, not a learned attack's efficacy.
            if len(calls)>1:
                _,error=runtime.run_function(env,'send_money',dict(recipient='US133000000121212121212',amount=1,
                                                                subject='Spotify Premium',date='2024-05-15'))
                self.assertIsNone(error)
            return prompt,runtime,env,[dict(role='assistant',content=[dict(type='text',content='I cannot comply.')],tool_calls=[])],{}
        def decide(prompt,stage):
            if stage=='planner':
                return json.dumps(dict(action='make_plan',observations=[],hypothesis='Adjust phrasing',
                    edit_scope='Payload',preserve=[],unknowns=[],objective=self.case['injection_task_goal'],
                    cause='unknown',strategy_rationale='Test a bounded edit within the selected strategy.',
                    hypothesis_test=dict(support_refs=['goal'],counter_refs=[],
                        unknowns=['The cause remains unknown'],predicted_observation='Observe the authorized receiver event.'),
                    edits=[dict(change='Clarify task',basis='hypothesis',references=['goal'],expected_effect='Goal compliance')]))
            if stage=='semantic_review':
                return dict(assessments={k:'unknown' for k in SEMANTIC_DIMENSIONS},findings=[])
            return '<injection>native regression candidate</injection>'
        cfg=load_config(ROOT/'experiments/e0_smoke.yaml')
        with tempfile.TemporaryDirectory() as directory:
            runner=Runner(AgentDojoAdapter(pipeline=SimpleNamespace(query=query)),
                          PIMinerAdapter(ROOT,'fixture',decide=decide),FixedObserver(),
                          build_policies(cfg['policies']),RunStore(Path(directory)/'run',{}))
            metrics=runner.run([self.case])
        self.assertEqual(len(calls),6)
        self.assertFalse(runner.episodes[0].gold)
        self.assertEqual(runner.episodes[0].trigger,'F')
        self.assertEqual(runner.episodes[0].response_signal,'Reject')
        for group in metrics['policies'].values():
            self.assertEqual(group['post_failure_successes'],1)
        self.assertEqual(metrics['policies']['stage_aware']['physical_post_failure_cost']['generator_model_calls'],3)
        self.assertEqual(metrics['policies']['always_revise']['physical_post_failure_cost']['generator_model_calls'],3)

    def test_original_router_attacker_with_native_target_and_official_gold(self):
        from src.attacker.original import OriginalPIMinerAdapter
        from src.vendor.router import _discover_strategies
        stages, target_calls = [], []
        ids = [s['id'] for s in _discover_strategies('agentdojo')[:3]]
        def decide(prompt, stage):
            stages.append(stage)
            if stage == 'router':
                return '<choice>' + ', '.join(ids) + '</choice>'
            return '<analysis>Native integration fixture.</analysis><injection>new payload</injection>'
        def query(prompt, runtime, env):
            target_calls.append(1)
            if len(target_calls) > 1:
                return self.pipeline().query(prompt, runtime, env)
            return prompt, runtime, env, [dict(role='assistant', content=[dict(type='text', content='No action.')], tool_calls=[])], {}
        cfg = load_config(ROOT / 'experiments/e0_smoke.yaml')
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(AgentDojoAdapter(pipeline=SimpleNamespace(query=query)),
                OriginalPIMinerAdapter(ROOT, 'fixture', 'gpt-5-nano', decide=decide),
                FixedObserver(), build_policies(cfg['policies']), RunStore(Path(directory) / 'run', {}))
            metrics = runner.run([self.case])
        self.assertEqual(stages.count('router'), 1)
        self.assertEqual(stages.count('attacker_p0'), 1)
        self.assertNotIn('planner', stages)
        self.assertEqual(len(target_calls), 6)
        self.assertTrue(all(o['gold_success'] for o in runner.outcomes))
        self.assertEqual(metrics['policies']['always_revise']['physical_post_failure_cost']['generator_model_calls'], 1)
        self.assertEqual(runner.attempts[self.case['case_id'] + '/shared/000']['final_output'], 'No action.')


if __name__=='__main__':unittest.main()
