"""Offline safeguards for a fixed 3-case x 2-entry x 5-repeat P0 probe."""
from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import httpx
import openai

from scripts.p0_repro_audit import HTTPAudit, record_pipeline, safe, write_json
from scripts import run_p0_stability as stability
from src.engine.config import ROOT


def json_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def completion():
    return dict(id='offline', object='chat.completion', created=0, model='gpt-5-nano',
        choices=[dict(index=0, finish_reason='stop', message=dict(role='assistant', content='OK'))],
        usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))


class HTTPAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.endpoint = 'https://offline.example/v1'

    def test_sdk_retries_are_captured_without_any_additional_send(self):
        attempts = []
        def handle(request):
            attempts.append(request)
            if len(attempts) < 3:
                return httpx.Response(500, json={'error': {'message': 'offline transient', 'type': 'server_error'}})
            return httpx.Response(200, json=completion())
        original_send = httpx.Client.send
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            sdk = openai.OpenAI(api_key='offline-placeholder', base_url=self.endpoint,
                                max_retries=2, http_client=http)
            with patch.object(sdk, '_calculate_retry_timeout', return_value=0), HTTPAudit(self.directory, self.endpoint) as audit:
                response = sdk.chat.completions.create(model='gpt-5-nano', messages=[{'role': 'user', 'content': 'offline test'}])
                self.assertEqual(response.choices[0].message.content, 'OK')
                self.assertEqual(len(attempts), 3)
                self.assertEqual(len(audit.records), 3)
                self.assertEqual(audit.summary()['http_send_attempts'], 3)
                self.assertEqual(audit.summary()['transport_attempts'], 3)
                self.assertEqual(audit.summary()['http_errors'], 2)
                self.assertEqual(audit.summary()['http_2xx'], 1)
        self.assertIs(httpx.Client.send, original_send)
        events = json_rows(self.directory / 'http_events.jsonl')
        self.assertEqual([event['status'] for event in events], ['started', 'response'] * 3)
        self.assertEqual([event['status_code'] for event in events if event['status'] == 'response'], [500, 500, 200])
        self.assertEqual(len({event['body_sha256'] for event in events}), 1)

    def test_http_audit_and_json_logs_never_persist_configured_credentials(self):
        api_key = 'OFFLINE_API_KEY_SENTINEL_123456'
        password = 'OFFLINE_PASSWORD_SENTINEL_123456'
        header_secret = 'HEADER_ONLY_SENTINEL'
        query_secret = 'QUERY_ONLY_SENTINEL'
        def handle(request):
            return httpx.Response(500, json={'error': {'message': api_key + ' ' + password},
                                             'authorization': 'another secret', 'api-key': api_key})
        with patch.dict(os.environ, {'OPENAI_API_KEY': api_key, 'TEST_PASSWORD': password}), \
                httpx.Client(transport=httpx.MockTransport(handle)) as http, \
                HTTPAudit(self.directory, self.endpoint):
            response = http.post(self.endpoint + '/chat/completions?api_key=' + query_secret,
                headers={'Authorization': 'Bearer ' + header_secret, 'X-Other-Key': header_secret},
                json={'model': 'offline', 'messages': [{'role': 'user', 'content': api_key}],
                      'api_key': api_key, 'nested': {'authorization': password}})
            self.assertEqual(response.status_code, 500)
            write_json(self.directory / 'safe.json', {'message': api_key, 'nested': {'password': password}})
        for path in self.directory.iterdir():
            saved = path.read_text()
            for secret in (api_key, password, header_secret, query_secret, 'another secret'):
                self.assertNotIn(secret, saved)
            self.assertIn('[REDACTED]', saved)
        request_event = json_rows(self.directory / 'http_events.jsonl')[0]
        self.assertNotIn('headers', request_event)
        self.assertEqual(request_event['path'], '/v1/chat/completions')

    def test_destination_boundary_and_transport_error_are_logged_without_retry(self):
        attempts = []
        def handle(request):
            attempts.append(request)
            raise httpx.ConnectError('do not persist arbitrary transport detail', request=request)
        original_send = httpx.Client.send
        with httpx.Client(transport=httpx.MockTransport(handle)) as http, HTTPAudit(self.directory, self.endpoint):
            for url in ('https://other.example/v1/chat/completions', self.endpoint + '/models'):
                with self.assertRaises(ValueError):
                    http.post(url, json={'model': 'offline'})
            self.assertEqual(attempts, [])
            with self.assertRaises(httpx.ConnectError):
                http.post(self.endpoint + '/chat/completions', json={'model': 'offline'})
        self.assertEqual(len(attempts), 1)
        self.assertIs(httpx.Client.send, original_send)
        events = json_rows(self.directory / 'http_events.jsonl')
        self.assertEqual([event['status'] for event in events], ['started', 'error'])
        self.assertEqual(events[-1]['error_type'], 'ConnectError')
        self.assertNotIn('arbitrary transport detail', (self.directory / 'http_events.jsonl').read_text())

    def test_same_route_redirects_are_counted_at_transport_level_without_extra_calls(self):
        attempts = []
        def handle(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(307, headers={'location': self.endpoint + '/chat/completions?hop=1'})
            return httpx.Response(200, json=completion())
        with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as http, \
                HTTPAudit(self.directory, self.endpoint) as audit:
            response = http.post(self.endpoint + '/chat/completions', json={'model': 'offline'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(audit.summary()['http_send_attempts'], 1)
            self.assertEqual(audit.summary()['transport_attempts'], 2)
            self.assertEqual(audit.records[0]['redirect_count'], 1)
        events = json_rows(self.directory / 'transport_events.jsonl')
        self.assertEqual([event['status_code'] for event in events if event['status'] == 'response'], [307, 200])

    def test_pipeline_audit_preserves_return_exception_and_query_count(self):
        class Pipeline:
            def __init__(self):
                self.calls = 0
            def query(self, prompt, runtime, env):
                self.calls += 1
                env['calls'] = self.calls
                if self.calls == 2:
                    raise RuntimeError('offline failure')
                return prompt, runtime, env, [{'role': 'assistant', 'content': 'offline'}], {}
        pipeline = Pipeline()
        original = pipeline.query
        env = {}
        with record_pipeline(pipeline, self.directory) as records:
            response = pipeline.query('offline', object(), env)
            self.assertIs(response[2], env)
            with self.assertRaises(RuntimeError):
                pipeline.query('offline', object(), env)
            self.assertEqual(pipeline.calls, 2)
            self.assertEqual([record['status'] for record in records], ['completed', 'error'])
        self.assertEqual(pipeline.query, original)
        self.assertEqual(len(json_rows(self.directory / 'pipeline_queries.jsonl')), 4)


class StabilityProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_exact_30_unique_trials_five_per_case_entry_and_balanced_pair_order(self):
        schedule = stability.make_schedule()
        self.assertEqual(len(schedule), 30)
        self.assertEqual(len({trial['trial_id'] for trial in schedule}), 30)
        self.assertEqual(set(trial['case_id'] for trial in schedule), {'c007', 'c017', 'c028'})
        self.assertEqual(Counter((trial['case_id'], trial['arm']) for trial in schedule),
                         Counter({(cid, arm): 5 for cid in stability.CASE_IDS for arm in stability.ARMS}))
        for cid in stability.CASE_IDS:
            local = [trial for trial in schedule if trial['case_id'] == cid]
            for repeat in range(1, 6):
                pair = [trial for trial in local if trial['repeat'] == repeat]
                self.assertEqual([trial['pair_position'] for trial in pair], [0, 1])
                self.assertEqual({trial['arm'] for trial in pair}, set(stability.ARMS))
                if repeat > 1:
                    previous = [trial for trial in local if trial['repeat'] == repeat - 1]
                    self.assertNotEqual(pair[0]['arm'], previous[0]['arm'])

    def test_all_30_executed_once_despite_successes_and_errors_without_replacement(self):
        calls = []
        schedule = stability.make_schedule()
        def execute(case, spec, directory, plan):
            calls.append(spec['trial_id'])
            if spec['repeat'] == 2:
                raise RuntimeError('offline trial error')
            return dict(valid=True, gold=spec['repeat'] == 1, error=None,
                        internal_query_count=1, http={'http_send_attempts': 1})
        cases = [dict(case_id=cid, p0='fixed offline P0', p0_sha256='offline') for cid in stability.CASE_IDS]
        with patch.object(stability, 'verify_frozen') as check:
            for case in cases:
                result = stability.run_case_trials(case, schedule, self.directory, {}, execute_fn=execute)
                self.assertEqual(result['completed'], 10)
            self.assertEqual(check.call_count, 30)
            self.assertEqual(len(calls), 30)
            self.assertEqual(len(set(calls)), 30)
            with self.assertRaises(FileExistsError):
                stability.run_case_trials(cases[0], schedule, self.directory, {}, execute_fn=execute)
            self.assertEqual(len(calls), 30)
        results = stability.collect_results(self.directory)
        self.assertEqual(len(results), 30)
        summary = stability.summarize_results(results)
        self.assertEqual((summary['successes'], summary['failures'], summary['errors'], summary['pending']), (6, 18, 6, 0))
        self.assertTrue(all(group['assigned'] == 5 and group['completed'] == 5 for group in summary['groups']))
        self.assertTrue(all(group['complete_success_rate'] is None for group in summary['groups']))

    def test_summary_keeps_legacy_false_errors_missing_and_rejects_duplicate_or_unknown_trials(self):
        schedule = stability.make_schedule()
        rows = [dict(spec, valid=False, gold=False, error='InternalServerError') for spec in schedule]
        summary = stability.summarize_results(rows)
        self.assertEqual(summary['errors'], 30)
        self.assertEqual(summary['failures'], 0)
        self.assertEqual(summary['successes'], 0)
        self.assertTrue(all(group['complete_success_rate'] is None for group in summary['groups']))
        partial = stability.summarize_results(rows[:1])
        self.assertEqual((partial['errors'], partial['pending']), (1, 29))
        for bad in (rows + [rows[0]], [dict(rows[0], trial_id='unknown')],
                    [dict(rows[0], case_id='c000')]):
            with self.assertRaises(ValueError):
                stability.summarize_results(bad)

    def test_plan_binds_inputs_p0_and_environment_without_hashing_source(self):
        digest = stability.digest
        def input_digest(path):
            self.assertNotEqual(Path(path).suffix, '.py', 'Code must never be fingerprinted')
            return digest(path)
        with patch.object(stability, 'digest', side_effect=input_digest):
            plan = stability.build_plan()
            stability.verify_frozen(plan)
        self.assertNotIn('source_hashes', plan)
        self.assertEqual([case['case_id'] for case in plan['cases']], list(stability.CASE_IDS))
        self.assertEqual(plan['top_level_executions'], 30)
        self.assertEqual(plan['protocol']['router_calls'], 0)
        self.assertEqual(plan['protocol']['attacker_calls'], 0)
        self.assertEqual(plan['protocol']['extra_live_preflight_calls'], 0)
        self.assertFalse(plan['protocol']['replacement_after_error'])
        frozen = set(plan['input_hashes'])
        archived = set(plan['source_paths'])
        expected = [ROOT / 'scripts/run_p0_stability.py', ROOT / 'scripts/p0_repro_audit.py',
                    ROOT / 'scripts/p0_repro_original.py', ROOT / 'src/adapters/agentdojo.py',
                    ROOT.parent / 'PIMiner/iterative_attack_orchestrator/iterative_attack_common.py',
                    ROOT.parent / 'PIMiner/benchmarks/agentdojo/reward.py']
        self.assertTrue({str(path.resolve()) for path in expected} <= archived)
        self.assertTrue(all(Path(path).suffix == '.py' for path in archived))
        self.assertTrue(all(Path(path).suffix != '.py' for path in frozen))
        self.assertIn(str(stability.LOCK.resolve()), frozen)
        self.assertIn(str((ROOT / 'data/piminer_aligned_20261030/initializations.json').resolve()), frozen)
        for case in plan['cases']:
            original = json.loads(Path(case['source_sample']).read_text())
            self.assertEqual(case['p0'], original['history'][0]['injection'])
            self.assertIn(case['source_sample'], frozen)
        drifted = deepcopy(plan)
        drifted['cases'][0]['p0'] += ' changed'
        with self.assertRaisesRegex(ValueError, 'P0 changed'):
            stability.verify_frozen(drifted)
        drifted = deepcopy(plan)
        drifted['schedule'].pop()
        with self.assertRaisesRegex(ValueError, 'schedule changed'):
            stability.verify_frozen(drifted)
        # A stale or deleted Python source in an old plan must not block use.
        legacy = deepcopy(plan)
        legacy['source_hashes'] = dict(legacy.pop('input_hashes'))
        legacy['source_hashes']['/nonexistent/obsolete_entry.py'] = 'obsolete fingerprint'
        legacy.pop('source_paths')
        with patch.object(stability, 'digest', side_effect=input_digest):
            stability.verify_frozen(legacy)
        changed = plan['cases'][0]['source_sample']
        with patch.object(stability, 'digest', side_effect=lambda path: 'changed' if str(path) == changed else input_digest(path)):
            for candidate in (plan, legacy):
                with self.assertRaisesRegex(ValueError, 'Frozen input changed'):
                    stability.verify_frozen(candidate)
        with patch.object(stability, 'current_environment', return_value=dict(plan['environment'], openai='changed')):
            with self.assertRaisesRegex(ValueError, 'Dependency environment changed'):
                stability.verify_frozen(plan)
        drifted = deepcopy(plan)
        drifted['environment']['openai'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'Dependency environment changed'):
            stability.verify_frozen(drifted)

    def test_source_archive_contains_copies_and_paths_without_fingerprints(self):
        source = self.directory / 'entry.py'
        source.write_text('print("offline source archive")\n')
        output = self.directory / 'run'
        output.mkdir()
        with patch.object(stability, 'digest', side_effect=AssertionError('Source hashing is forbidden')):
            stability.archive_sources(output, {'source_paths': [str(source)]})
        records = json.loads((output / 'source_snapshot.json').read_text())
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]), {'source', 'snapshot'})
        self.assertEqual((output / records[0]['snapshot']).read_bytes(), source.read_bytes())

    def test_execute_trial_uses_original_valid_flag_not_legacy_false_verdict(self):
        spec = stability.make_schedule()[0]
        self.assertEqual(spec['arm'], 'original')
        case = dict(case_id=spec['case_id'], p0='offline', p0_sha256='offline')
        plan = dict(original_root=str(ROOT.parent / 'PIMiner'))
        for index, output in enumerate((dict(valid=True, gold=True, utility=True, error=None, internal_queries=[{}]),
                                        dict(valid=False, gold=False, utility=False, error=None,
                                             execution_error='NoFinalOutput', internal_queries=[{}, {}, {}]))):
            directory = self.directory / str(index)
            directory.mkdir()
            with patch('p0_repro_original.OriginalP0Runner') as runner:
                runner.return_value.execute.return_value = output
                result = stability.execute_trial(case, spec, directory, plan)
            self.assertEqual(result['valid'], output['valid'])
            self.assertEqual(result['gold'], True if output['valid'] else None)
            self.assertEqual(result['internal_query_count'], len(output['internal_queries']))
            self.assertEqual(result['http']['http_send_attempts'], 0)
            if not output['valid']:
                self.assertEqual(result['error'], 'NoFinalOutput')


class OriginalEntryOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from src.engine.config import load_config, load_cases
        cls.case = next(case for case in load_cases(load_config(
            ROOT / 'experiments/evidence_piminer_aligned_asr5_20261030.yaml')) if case['case_id'] == 'c017')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def runner(self, factory):
        from scripts.p0_repro_original import OriginalP0Runner
        return OriginalP0Runner(ROOT.parent / 'PIMiner', self.directory, reward_factory=factory)

    def test_original_reentry_reuses_environment_but_next_top_level_execution_is_fresh(self):
        from agentdojo.task_suite.load_suites import get_suite
        suite = get_suite('v1.2.2', 'banking')
        pipelines = []
        class Pipeline:
            def __init__(self):
                self.envs = []
            def query(self, prompt, runtime, env):
                self.envs.append(env)
                if len(self.envs) == 1:
                    error = RuntimeError('offline recoverable internal error')
                    error.messages = [dict(role='assistant', content=None, tool_calls=[])]
                    raise error
                return prompt, runtime, env, [dict(role='assistant', content=[dict(type='text', content='Done.')], tool_calls=[])], {}
        def factory(**kwargs):
            pipeline = Pipeline()
            pipelines.append(pipeline)
            return types.SimpleNamespace(pipeline=pipeline, _get_suite=lambda name: suite)
        runner = self.runner(factory)
        outputs = [runner.execute(self.case, 'offline inert payload', f'offline-{i}') for i in range(2)]
        self.assertEqual(len(pipelines), 2)
        for pipeline, output in zip(pipelines, outputs):
            self.assertEqual(len(pipeline.envs), 2)
            self.assertIs(pipeline.envs[0], pipeline.envs[1])
            self.assertEqual(output['pipeline_query_count'], 2)
            self.assertEqual(output['internal_error_count'], 1)
            self.assertTrue(output['valid'])
            self.assertIsNone(output.get('error'))
        self.assertIsNot(pipelines[0].envs[0], pipelines[1].envs[0])

    def test_original_swallowed_errors_without_final_are_missing_despite_legacy_false(self):
        from agentdojo.task_suite.load_suites import get_suite
        suite = get_suite('v1.2.2', 'banking')
        calls = []
        def query(*args):
            calls.append(1)
            error = RuntimeError('offline failure')
            error.messages = [dict(role='assistant', content=None, tool_calls=[])]
            raise error
        runner = self.runner(lambda **kwargs: types.SimpleNamespace(
            pipeline=types.SimpleNamespace(query=query), _get_suite=lambda name: suite))
        result = runner.execute(self.case, 'offline inert payload', 'offline-error')
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['pipeline_query_count'], 3)
        self.assertEqual(result['internal_error_count'], 3)
        self.assertFalse(result['valid'])
        self.assertFalse(result['gold'])
        self.assertTrue(result.get('error') or result.get('execution_error'))


    def test_original_plain_exception_stays_missing_when_native_helper_aborts_early(self):
        from agentdojo.task_suite.load_suites import get_suite
        suite = get_suite('v1.2.2', 'banking')
        calls = []
        def query(*args):
            calls.append(1)
            raise RuntimeError('offline exception without messages')
        runner = self.runner(lambda **kwargs: types.SimpleNamespace(
            pipeline=types.SimpleNamespace(query=query), _get_suite=lambda name: suite))
        result = runner.execute(self.case, 'offline inert payload', 'offline-plain-error')
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['internal_error_count'], 1)
        self.assertFalse(result['valid'])
        self.assertFalse(result['gold'])
        self.assertEqual(result['error'], 'IndexError')
        self.assertEqual(result['internal_queries'][0]['error_type'], 'RuntimeError')

    def test_real_original_factory_and_native_pipeline_are_used_without_network(self):
        from openai.types.chat import ChatCompletion
        from scripts.p0_repro_original import OriginalP0Runner
        requests = []
        def create(**kwargs):
            requests.append(kwargs)
            return ChatCompletion.model_validate(completion())
        client = types.SimpleNamespace(max_retries=2, timeout='SDK default',
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        with patch('openai.OpenAI', return_value=client) as factory:
            runner = OriginalP0Runner(ROOT.parent / 'PIMiner', self.directory)
            result = runner.execute(self.case, 'offline inert payload', 'offline-real-factory')
        factory.assert_called_once_with()
        self.assertTrue(result['valid'])
        self.assertEqual(result['pipeline_query_count'], 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(result['api_calls']), 1)
        self.assertEqual(result['tool_loop_bounds'], [15])


if __name__ == '__main__':
    unittest.main()
