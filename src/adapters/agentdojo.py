"""One native AgentDojo rollout and the original suite's official checkers.

No reward wrapper that turns exceptions into misses, and no implicit trajectory
retries. Every execute loads and initializes a fresh environment.
"""
import json
import os
import re
import time
from ..schema import TargetResult
from ..evidence.observer import policy_for_case
from ..vendor.attacker_observer import Collector
from ..engine.storage import plain
from ..attacker.call_logging import begin_call, dispatch_call, finish_call


def _error_attribute(value, name):
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _api_error_details(exc, sensitive_texts=(), api_key_env='OPENAI_API_KEY'):
    """Bounded status diagnostics, never response bodies, headers or request data."""
    from openai import APIStatusError
    if not isinstance(exc, APIStatusError):
        return {}
    secrets = [value for name, value in os.environ.items()
               if value and (name == api_key_env or re.search(r'KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL', name, re.I))]
    # Payloads and task prompts can be echoed by a provider. Redact original and
    # JSON-escaped forms, including individual lines, before limiting the output.
    private = []
    for value in (*secrets, *sensitive_texts):
        if isinstance(value, str) and value:
            private.extend((value, json.dumps(value, ensure_ascii=False)[1:-1],
                            json.dumps(value, ensure_ascii=True)[1:-1]))
            private.extend(line for line in value.splitlines() if len(line) >= 16)
    private = sorted(set(private), key=len, reverse=True)

    def limited(value, limit):
        if not isinstance(value, str) or not value:
            return None
        for secret in private:
            value = value.replace(secret, '[REDACTED]')
        # Do not try to parse or retain embedded requests/headers in an error.
        if re.search(r'(?i)["\']?(?:messages|prompt|payload|headers|authorization)["\']?\s*[:=]', value):
            return '[request content omitted]'
        value = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', value)
        value = re.sub(r'''(?i)["']?\b(?:api[_-]?key|access[_-]?token|password|secret)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
                       '[credential redacted]', value)
        value = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED]', value)
        value = re.sub(r'[\x00-\x1f\x7f]', ' ', value)
        return value[:limit] + ('…' if len(value) > limit else '')

    details = {}
    status = _error_attribute(exc, 'status_code')
    if type(status) is int and 100 <= status <= 599:
        details['status_code'] = status
    request_id = limited(_error_attribute(exc, 'request_id'), 128)
    if request_id is not None:
        details['request_id'] = request_id
    body = _error_attribute(exc, 'body')
    provider = body.get('error', body) if isinstance(body, dict) else None
    if not isinstance(provider, dict):
        provider = {}
    for output, field, limit in (('provider_code', 'code', 128), ('provider_type', 'type', 128), ('message', 'message', 512)):
        value = provider.get(field)
        if value is None:
            value = _error_attribute(exc, field)
        if field == 'code' and type(value) is int:
            value = str(value)
        sanitized = limited(value, limit)
        if sanitized is not None:
            details[output] = sanitized
    return details


def _record_api_error(record, exc, sensitive_texts=(), api_key_env='OPENAI_API_KEY'):
    # Diagnostic extraction must never mask the original error or change retries.
    try:
        details = _api_error_details(exc, sensitive_texts, api_key_env)
        if details:
            record['error_details'] = details
    except Exception:
        pass


def official_scores(suite, user, injection, output, pre, post, trace):
    """Single scoring entry for live execution and offline score regression."""
    utility = bool(suite._check_task_result(user, output, pre, post, trace))
    gold = bool(suite._check_task_result(injection, output, pre, post, trace))
    return utility, gold


def validate_native_cases(cases, benchmark_version='v1.2.2'):
    """Offline task/vector and payload-template check, before any model calls."""
    import agentdojo.default_suites
    from agentdojo.task_suite.load_suites import get_suite
    for case in cases:
        suite = get_suite(benchmark_version, case['suite_name'])
        user = suite.get_user_task_by_id(case['user_task_id'])
        injection = suite.get_injection_task_by_id(case['injection_task_id'])
        if case['user_task_prompt'] != user.PROMPT or case['injection_task_goal'] != injection.GOAL:
            raise ValueError('Case public task differs from native version: ' + case['case_id'])
        if set(case['injection_candidates']) - set(suite.get_injection_vector_defaults()):
            raise ValueError('Unknown native injection vector: ' + case['case_id'])
        text = case.get('p0') or 'Template validation placeholder'
        env = suite.load_and_inject_default_environment(
            {key: json.dumps(text, ensure_ascii=False)[1:-1] for key in case['injection_candidates']})
        user.init_environment(env)
    return dict(status='passed', cases=len(cases), api_calls=0)


class AgentDojoAdapter:
    def __init__(self, model='gpt-5-nano', benchmark_version='v1.2.2', reasoning_effort=None,
                 base_url_env='OPENAI_BASE_URL', api_key_env='OPENAI_API_KEY',
                 request_timeout=120, max_tool_calls=30, pipeline=None, active_verification=None,
                 probe_sampling=None):
        import agentdojo.default_suites  # registers native suites
        from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline, PipelineConfig
        from agentdojo.agent_pipeline.llms.openai_llm import OpenAILLM
        import openai
        self.version = benchmark_version
        self.model = model
        self.api_calls = []
        self._error_sensitive_texts = ()
        self._api_key_env = api_key_env
        self.llm = None
        self.active_verification = None
        self.probe_sampling = None
        if active_verification is not None:
            from ..evidence.active_verification import validate_config
            self.active_verification = validate_config(active_verification)
        if probe_sampling is not None:
            from ..evidence.probe_sampling import validate_channel_config
            self.probe_sampling = validate_channel_config(probe_sampling)
        if self.active_verification is not None and self.probe_sampling is not None:
            raise ValueError('Framework verification and jailbreak probe sampling are separate protocols')
        self._http_stage = 'target_http'
        self.max_tool_calls = max_tool_calls
        if type(max_tool_calls) is not int or max_tool_calls < 1:
            raise ValueError('max_tool_calls must be positive')
        if pipeline is None:
            client = openai.OpenAI(api_key=os.environ.get(api_key_env),
                                   base_url=os.environ.get(base_url_env),
                                   timeout=request_timeout, max_retries=0)
            if hasattr(client, '_client'):
                # Keep each recorded create invocation to one HTTP dispatch;
                # SDK retries are disabled above and redirects must be explicit.
                client._client.follow_redirects = False
            original = client.chat.completions.create

            def create(*args, **kwargs):
                item = dict(model=model, stage=self._http_stage, status='running', usage=None)
                self.api_calls.append(item)
                started = time.monotonic()
                try:
                    begin_call(item, self._http_stage)
                    if model.startswith('deepseek'):
                        kwargs['messages'] = [dict(m, role='system') if m.get('role') == 'developer' else m
                                              for m in kwargs['messages']]
                    dispatch_call(item)
                    response = original(*args, **kwargs)
                    item.update(status='completed', usage=plain(response.usage),
                                response_model=response.model)
                    return response
                except Exception as exc:
                    item.update(status='error', error_type=type(exc).__name__)
                    _record_api_error(item, exc, self._error_sensitive_texts, self._api_key_env)
                    details = item.get('error_details', {})
                    if details.get('status_code') is not None:
                        item['status_code'] = details['status_code']
                    raise
                finally:
                    item['seconds'] = time.monotonic() - started
                    finish_call(item)
            from .backoff import rate_limit_retry
            client.chat.completions.create = lambda *a, **kw: rate_limit_retry(create, *a, **kw)
            llm = OpenAILLM(client, model)
            llm.name = model
            if reasoning_effort is not None:
                llm.reasoning_effort = reasoning_effort
            self.llm = llm
            pipeline = AgentPipeline.from_config(PipelineConfig(llm=llm, model_id=None, defense=None,
                                                                system_message_name=None, system_message=None))
            # AgentDojo bounds its tool execution loop. Freeze the bound in config.
            self._set_loop_bound(pipeline, max_tool_calls)
        self.pipeline = pipeline

    @staticmethod
    def _set_loop_bound(node, limit):
        if hasattr(node, 'max_iters'):
            node.max_iters = limit
        for child in getattr(node, 'elements', []) or []:
            AgentDojoAdapter._set_loop_bound(child, limit)

    def preflight(self):
        from agentdojo.functions_runtime import FunctionsRuntime
        if self.llm is None:
            return dict(status='offline_pipeline', calls=[])
        start = len(self.api_calls)
        self.llm.query('Reply with OK.', FunctionsRuntime([]),
                       messages=[dict(role='user', content=[dict(type='text', content='Reply with OK.')])])
        return dict(status='passed', calls=self.api_calls[start:])

    def _resolve_tasks(self, case):
        from agentdojo.task_suite.load_suites import get_suite
        suite = get_suite(self.version, case['suite_name'])
        return (suite, suite.get_user_task_by_id(case['user_task_id']),
                suite.get_injection_task_by_id(case['injection_task_id']))

    def _score_tasks(self, suite, user, injection, output, pre, post, trace, case, raw):
        return official_scores(suite, user, injection, output, pre, post, trace)

    def execute(self, case, payload, episode_id):
        from agentdojo.task_suite.load_suites import get_suite
        from agentdojo.task_suite.task_suite import model_output_from_messages, functions_stack_trace_from_messages
        from agentdojo.functions_runtime import FunctionsRuntime
        from agentdojo.types import get_text_content_as_str
        start = time.monotonic()
        call_start = len(self.api_calls)
        messages = []
        raw = dict(episode_id=episode_id)
        self._error_sensitive_texts = (payload.text, case.get('user_task_prompt', ''))
        result = None
        try:
            suite, user, injection = self._resolve_tasks(case)
            verification = self.active_verification
            probe = self.probe_sampling
            if verification and (case['suite_name'] != 'workspace' or
                    not {'get_sent_emails', 'list_files'} <= {f.name for f in suite.tools}):
                raise ValueError('Active verification v1 requires native workspace list tools')
            if probe and (case['suite_name'] != 'workspace' or
                    not {'get_sent_emails', 'list_files', 'send_email'} <=
                    {f.name for f in suite.tools}):
                raise ValueError('Probe sampling requires native workspace mail and drive tools')
            if case['user_task_prompt'] != user.PROMPT or case['injection_task_goal'] != injection.GOAL:
                raise ValueError('Public task does not match native task; composed schemas need their own adapter')
            vectors = case['injection_candidates']
            if not vectors or set(vectors) - set(suite.get_injection_vector_defaults()):
                raise ValueError('Unknown or empty injection vectors')
            # Native YAML templates expect JSON-escaped scalar contents, as in the original submit path.
            injections = {v: json.dumps(payload.text, ensure_ascii=False)[1:-1] for v in vectors}
            env = suite.load_and_inject_default_environment(injections)
            env = user.init_environment(env)
            pre = env.model_copy(deep=True)
            collector = Collector.for_native_rollout(policy_for_case(case),
                benchmark_version=self.version, suite_name=case['suite_name'])
            tools = collector.wrap_tools(suite.tools)
            channel = None
            if probe:
                from ..evidence.probe_sampling import ProbeChannel
                channel = ProbeChannel(probe, episode_id=episode_id,
                                       account_email=env.inbox.account_email)
                channel.validate_pre(plain(pre))
                tools = channel.wrap_tools(tools)
            runtime = FunctionsRuntime(tools)
            prompt = user.PROMPT
            if verification and verification['mode'] == 'embedded':
                from ..evidence.active_verification import VerificationRuntime, verification_prompt
                runtime = VerificationRuntime(list(runtime.functions.values()), verification, readonly=False)
                prompt += '\n\n' + verification_prompt(verification)
            _, _, post, messages, _ = self.pipeline.query(prompt, runtime, env)
            output = model_output_from_messages(messages)
            if output is None:
                raise RuntimeError('Target ended without a final model output')
            final = get_text_content_as_str(output) if isinstance(output, list) else str(output)
            trace = functions_stack_trace_from_messages(messages)
            # Capture before official checkers (some checkers mutate getters).
            raw.update(pre=plain(pre), post=plain(post), messages=plain(messages))
            if channel is not None:
                raw['probe_sampling'] = channel.report(
                    raw['pre'], raw['post'], receipts=collector.receipts,
                    status='completed')
            verification_env = post.model_copy(deep=True) if verification else None
            utility, gold = self._score_tasks(suite, user, injection, output, pre, post, trace, case, raw)
            observation, observation_error = None, None
            try:
                observation = collector.finish(final, rollout_complete=True)
            except Exception as exc:
                observation_error = type(exc).__name__
            result = TargetResult(gold=gold, utility=utility, final=final, observation=observation,
                                  observation_error=observation_error)
            from ..vendor.final_output import extract_final_output
            result.black_box_final = extract_final_output(messages)
            if verification:
                # Verification failure cannot erase the completed rollout or its score.
                try:
                    from ..evidence.active_verification import run_followup
                    if verification['mode'] == 'embedded':
                        raw['active_verification'] = runtime.report(status='completed', final=final,
                            model_rounds=None, messages=[])
                    else:
                        verification_call_start = len(self.api_calls)
                        self._http_stage = 'active_verification_http'
                        raw['active_verification'] = run_followup(
                            self.llm, suite.tools, verification_env, messages, verification)
                        raw['active_verification']['api_calls'] = len(self.api_calls) - verification_call_start
                except Exception as exc:
                    raw['active_verification'] = dict(status='error', error=type(exc).__name__,
                                                     facts=[], receipts=[])
                finally:
                    self._http_stage = 'target_http'
        except Exception as exc:
            raw.update(messages=plain(messages), error_type=type(exc).__name__)
            _record_api_error(raw, exc, self._error_sensitive_texts, self._api_key_env)
            result = TargetResult(gold=None, error=type(exc).__name__)
        result.seconds = time.monotonic() - start
        calls = self.api_calls[call_start:]
        raw['api_calls'] = calls
        known = [c['usage'] for c in calls if c.get('usage')]
        if calls:
            result.usage = dict(api_calls=len(calls), missing_usage_calls=len(calls) - len(known),
                                input_tokens=sum(u.get('prompt_tokens', 0) for u in known),
                                output_tokens=sum(u.get('completion_tokens', 0) for u in known))
        result.raw = raw
        self._error_sensitive_texts = ()
        return result
