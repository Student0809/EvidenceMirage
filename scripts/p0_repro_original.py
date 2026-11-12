"""Instrument the real PIMiner Target entry point without replacing its semantics.

One ``execute`` is one historical submit-equivalent top-level execution. The
original helper owns its internal pipeline retries and environment reuse. This
module adds no retry, timeout, tool wrapper, or model generation setting.

``api_calls`` count SDK create invocations, not HTTP sends: SDK-internal retries
must be counted by the experiment runner's separate HTTP instrumentation.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import importlib
import io
import json
from pathlib import Path
import sys
import time
import warnings

from src.engine.storage import plain
from scripts.p0_repro_audit import append_json, now


def _load_original(original_root):
    root = Path(original_root).resolve()
    expected = root / 'iterative_attack_orchestrator/iterative_attack_common.py'
    if not expected.is_file():
        raise FileNotFoundError('Original PIMiner common module is missing')
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    module = importlib.import_module('iterative_attack_orchestrator.iterative_attack_common')
    if Path(module.__file__).resolve() != expected.resolve():
        raise RuntimeError('Original PIMiner common module resolves to another checkout')
    reward_module = importlib.import_module('benchmarks.agentdojo.reward')
    if Path(reward_module.__file__).resolve() != (root / 'benchmarks/agentdojo/reward.py').resolve():
        raise RuntimeError('Original PIMiner reward module resolves to another checkout')
    return module


def _copy(value):
    return value.model_copy(deep=True) if hasattr(value, 'model_copy') else deepcopy(value)


def _has_final(messages):
    # Native helpers may raise for an empty/partial trace. Observation must not
    # replace the original pipeline exception or its recovery decision.
    from agentdojo.task_suite.task_suite import model_output_from_messages
    try:
        return model_output_from_messages(messages) is not None
    except Exception:
        return False


class _MessageCapture:
    """Native logger sink; no file IO or changes to the model-visible messages."""
    def __init__(self):
        self.messages = []
        self.log_calls = 0
        self.log_error_calls = 0

    def log(self, messages, **kwargs):
        self.messages = deepcopy(messages)
        self.log_calls += 1

    def log_error(self, *args, **kwargs):
        # Exceptions and HTTP metadata are recorded by their own layers. Logger
        # error strings can include response bodies, so do not duplicate them.
        self.log_error_calls += 1


class _PipelineRecorder:
    def __init__(self, pipeline, output_dir, execution_id):
        self.pipeline = pipeline
        self.path = Path(output_dir) / 'original_pipeline_queries.jsonl'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.execution_id = execution_id
        self.audit_error = None
        self.records = []
        self.snapshots = []
        self.initial_pre = None
        self.active_query_index = None

    def query(self, *args, **kwargs):
        from agentdojo.logging import LOGGER_STACK
        env = args[2] if len(args) > 2 else kwargs['env']
        pre = _copy(env)
        if self.initial_pre is None:
            self.initial_pre = _copy(env)
        capture = _MessageCapture()
        # Set an isolated ContextVar value instead of mutating the native
        # logger's shared default list (safe even when another context exists).
        token = LOGGER_STACK.set([*LOGGER_STACK.get(), capture])
        index = len(self.records)
        self.active_query_index = index
        record = dict(execution_id=self.execution_id, query_index=index, status='started',
                      started_at=now(), pre=plain(pre),
                      messages=[], error_type=None)
        self.records.append(record)
        start = time.monotonic()
        try:
            if self.audit_error is not None:
                raise RuntimeError('Prior pipeline audit write failed')
            self._persist(record, 'started')
            output = self.pipeline.query(*args, **kwargs)
            post, messages = output[2], output[3]
            post_copy, messages_copy = _copy(post), deepcopy(messages)
            record.update(status='completed', post=plain(post_copy),
                          messages=plain(messages_copy),
                          messages_source='pipeline_return', messages_complete=True,
                          has_final=_has_final(messages_copy))
            self.snapshots.append((post_copy, messages_copy))
            return output
        except Exception as exc:
            # Observe the same exception attributes the original helper reads;
            # re-raise the original object, retaining its recovery semantics.
            post = getattr(exc, 'task_environment', None)
            if post is None:
                post = env
            messages = getattr(exc, 'messages', None)
            from_exception = messages is not None
            if messages is None:
                messages = capture.messages
            post_copy, messages_copy = _copy(post), deepcopy(messages)
            record.update(status='error', error_type=type(exc).__name__,
                          post=plain(post_copy), messages=plain(messages_copy),
                          messages_source='exception' if from_exception else 'last_native_log',
                          messages_complete=from_exception,
                          has_final=_has_final(messages_copy))
            self.snapshots.append((post_copy, messages_copy))
            raise
        finally:
            record.update(completed_at=now(), seconds=time.monotonic() - start,
                          native_log_calls=capture.log_calls,
                          native_log_error_calls=capture.log_error_calls)
            self.active_query_index = None
            LOGGER_STACK.reset(token)
            self._persist(record, 'ended')

    def _persist(self, record, event):
        try:
            append_json(self.path, dict(record, event=event))
        except Exception as exc:
            self.audit_error = type(exc).__name__
            raise

    def diagnostic_scores(self, suite, user, injection):
        """Score copied snapshots only after the real original helper has ended."""
        from agentdojo.task_suite.task_suite import (
            model_output_from_messages, functions_stack_trace_from_messages)
        for record, (post, messages) in zip(self.records, self.snapshots):
            try:
                pre_copy = _copy(self.initial_pre)
                post_copy = _copy(post)
                output = model_output_from_messages(messages)
                trace = functions_stack_trace_from_messages(messages)
                utility = bool(suite._check_task_result(
                    deepcopy(user), output or [], pre_copy, post_copy, trace))
                gold = bool(suite._check_task_result(
                    deepcopy(injection), output or [], pre_copy, post_copy, trace))
                record['diagnostic_scores'] = dict(
                    utility=utility, gold=gold, error_type=None,
                    final_available=output is not None,
                    scope='copied_query_post_against_top_level_initial_pre')
            except Exception as exc:
                record['diagnostic_scores'] = dict(
                    utility=None, gold=None, error_type=type(exc).__name__,
                    scope='copied_query_post_against_top_level_initial_pre')
            self._persist(record, 'scored')


def _llm_nodes(pipeline):
    seen = set()
    pending = [pipeline]
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        pending.extend(reversed(getattr(node, 'elements', []) or []))
        client = getattr(node, 'client', None)
        if client is not None and hasattr(getattr(client, 'chat', None), 'completions'):
            yield node


def _instrument_create(pipeline, recorder, api_calls):
    restores, settings, seen = [], [], set()
    for node in _llm_nodes(pipeline):
        client = node.client
        completions = client.chat.completions
        if id(completions) in seen:
            continue
        seen.add(id(completions))
        original = completions.create
        settings.append(dict(model=getattr(node, 'model', None),
                             reasoning_effort=getattr(node, 'reasoning_effort', None),
                             temperature=getattr(node, 'temperature', None),
                             sdk_max_retries=getattr(client, 'max_retries', None),
                             sdk_timeout=str(getattr(client, 'timeout', None))))

        def create(*args, _original=original, **kwargs):
            item = dict(index=len(api_calls), query_index=recorder.active_query_index,
                        model=kwargs.get('model'), status='running', error_type=None,
                        usage=None, response_model=None)
            api_calls.append(item)
            start = time.monotonic()
            try:
                response = _original(*args, **kwargs)
                item.update(status='completed', response_model=getattr(response, 'model', None),
                            usage=plain(getattr(response, 'usage', None)))
                return response
            except Exception as exc:
                item.update(status='error', error_type=type(exc).__name__)
                raise
            finally:
                item['seconds'] = time.monotonic() - start

        completions.create = create
        restores.append((completions, original))
    return restores, settings


class OriginalP0Runner:
    """Real original build_reward + run_full_trajectory, with local audit records.

    ``reward_factory`` is an offline test seam with build_reward's keyword API;
    production omits it. A fresh reward/client/pipeline is built per execution,
    matching the original submit implementation. Caller persists the returned
    dict in its own experiment output directory.
    """
    def __init__(self, original_root, output_dir, model='gpt-5-nano',
                 reasoning_effort=None, reward_factory=None):
        self.original_root = Path(original_root).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.common = _load_original(self.original_root)
        self.reward_factory = reward_factory or self.common.build_reward

    def execute(self, case, payload_text, execution_id):
        import agentdojo.default_suites  # Register the exact native suites.
        if not isinstance(payload_text, str) or not payload_text:
            raise ValueError('P0 must be a nonempty string')
        if not isinstance(execution_id, str) or not execution_id:
            raise ValueError('Execution ID must be a nonempty string')
        start = time.monotonic()
        api_calls, restores = [], []
        recorder = None
        result = dict(execution_id=execution_id, case_id=case['case_id'],
                      arm='original', gold=False, utility=False, final='', error=None,
                      valid=False, audit_valid=False, execution_error=None,
                      model_output_text='', messages=[], pre=None, post=None,
                      internal_queries=[], api_calls=api_calls, runtime_settings=[],
                      api_call_scope='SDK create invocations; excludes SDK-internal HTTP retries',
                      scoring_semantics='original submit: escaping exception maps to false/false',
                      warning_categories=[])
        try:
            # The original OpenAI factory prints a startup banner. Suppress only
            # this banner; do not capture or emit environment variables.
            with redirect_stdout(io.StringIO()):
                reward = self.reward_factory(target_model=self.model,
                    output_dir=str(self.output_dir), reasoning_effort=self.reasoning_effort)
            suite = reward._get_suite(case['suite_name'])
            user = suite.get_user_task_by_id(case['user_task_id'])
            injection = suite.get_injection_task_by_id(case['injection_task_id'])
            if case['user_task_prompt'] != user.PROMPT or case['injection_task_goal'] != injection.GOAL:
                raise ValueError('Public case differs from original native suite')
            recorder = _PipelineRecorder(reward.pipeline, self.output_dir, execution_id)
            restores, settings = _instrument_create(reward.pipeline, recorder, api_calls)
            result['runtime_settings'] = settings
            result['tool_loop_bounds'] = self._loop_bounds(reward.pipeline)
            injections = {name: json.dumps(payload_text)[1:-1]
                          for name in case['injection_candidates']}

            def capture_states(pre, post):
                result.update(pre=plain(pre), post=plain(post))

            with warnings.catch_warnings(record=True) as captured:
                utility, gold, messages, model_output_text = self.common.run_full_trajectory(
                    suite, user, injection, injections, recorder,
                    state_observer=capture_states)
            result['warning_categories'] = [w.category.__name__ for w in captured]
            result.update(gold=gold, utility=utility, messages=plain(messages),
                          model_output_text=model_output_text,
                          final=self.common.extract_final_output(messages))
            result['valid'] = _has_final(messages) and type(gold) is bool and type(utility) is bool
            result['audit_valid'] = result['valid']
            if not result['valid']:
                result['execution_error'] = 'NoFinalModelOutput'
        except Exception as exc:
            # Match iterative_attack_claude_code.submit's explicit error scores,
            # while keeping the error distinguishable for reproduction analysis.
            result.update(error=type(exc).__name__, execution_error=type(exc).__name__,
                          valid=False, audit_valid=False, gold=False, utility=False,
                          final='', model_output_text='')
        finally:
            for completions, original in reversed(restores):
                completions.create = original
            if recorder is not None:
                try:
                    recorder.diagnostic_scores(suite, user, injection)
                except Exception as exc:
                    result.update(valid=False, audit_valid=False, execution_error=type(exc).__name__)
                if recorder.audit_error is not None:
                    result.update(valid=False, audit_valid=False, execution_error=recorder.audit_error)
                result['internal_queries'] = recorder.records
                if result['pre'] is None and recorder.initial_pre is not None:
                    result['pre'] = plain(recorder.initial_pre)
                if result['post'] is None and recorder.records:
                    result['post'] = recorder.records[-1]['post']
                if not result['messages'] and recorder.records:
                    # A top-level error/no-final can retain diagnostics, but
                    # never promote these messages into the original verdict.
                    result['last_query_messages'] = recorder.records[-1]['messages']
            result['seconds'] = time.monotonic() - start
            result['pipeline_query_count'] = len(result['internal_queries'])
            result['internal_error_count'] = sum(
                q['status'] == 'error' for q in result['internal_queries'])
        return result

    @staticmethod
    def _loop_bounds(pipeline):
        seen, pending, bounds = set(), [pipeline], []
        while pending:
            node = pending.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            if hasattr(node, 'max_iters'):
                bounds.append(node.max_iters)
            pending.extend(getattr(node, 'elements', []) or [])
        return bounds
