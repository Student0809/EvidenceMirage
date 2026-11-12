"""Failure accounting tests use simulated processes/HTTP, never paid models."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import openai
import pytest

from src.attacker.call_logging import CallsStopped
from src.attacker.transport import RoleDecision
from src.vendor.refinement import ClaudeDecision
from src.attacker.session import PersistentRoleDecision
from src.adapters.agentdojo import AgentDojoAdapter


@pytest.mark.parametrize('kind', ['role', 'grounded', 'persistent'])
def test_nonzero_cli_preserves_partial_usage_and_stops_next_dispatch(tmp_path, kind):
    log, stop = tmp_path / 'calls.jsonl', tmp_path / 'stop.json'
    with patch.dict(os.environ, PIMINER_CALL_LOG=str(log), PIMINER_STOP_FILE=str(stop)):
        if kind == 'role':
            decision = RoleDecision('fixture', 'max', 1, 'PRIVATE_PROMPT')
            module = 'src.attacker.transport'
        elif kind == 'grounded':
            decision = ClaudeDecision('fixture', 'max', 1)
            module = 'src.vendor.refinement'
        else:
            decision = PersistentRoleDecision('fixture', 'max', 1, 'PRIVATE_PROMPT', tmp_path / 'session')
            module = 'src.attacker.session'
        envelope = json.dumps(dict(is_error=True, result='API Error: 402 PRIVATE_RESPONSE',
                                   usage={'input_tokens': 11}, total_cost_usd=0.003))
        process = SimpleNamespace(returncode=1, poll=lambda: 1,
                                  communicate=lambda *a, **k: (envelope, 'PRIVATE_STDERR'))
        with patch(module + '.subprocess.Popen', return_value=process):
            with pytest.raises(RuntimeError):
                decision('PRIVATE_REQUEST', 'preflight')
        record = decision.calls[0]
        assert record['status_code'] == 402
        assert record['usage'] == {'input_tokens': 11}
        assert record['reported_cost_usd'] == .003
        assert record['cost_usd'] is None
        assert stop.exists()
        with patch('src.attacker.transport.subprocess.Popen') as popen:
            with pytest.raises(CallsStopped):
                RoleDecision('fixture', 'max', 1, '')('no dispatch', 'preflight')
            popen.assert_not_called()
        saved = log.read_text()
        assert 'PRIVATE_' not in saved
        rows = [json.loads(line) for line in saved.splitlines()]
        assert sum(row['event'] == 'dispatch' for row in rows) == 1
        assert rows[2]['http_requests'] is None


def test_target_402_is_durable_before_any_later_http_call(tmp_path):
    request = httpx.Request('POST', 'https://fixture.invalid')
    error = openai.APIStatusError('PRIVATE_RESPONSE', response=httpx.Response(402, request=request),
                                 body={'error': {'type': 'insufficient_balance'}})
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        raise error
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with patch.dict(os.environ, PIMINER_CALL_LOG=str(tmp_path / 'calls.jsonl'),
                    PIMINER_STOP_FILE=str(tmp_path / 'stop.json')), \
            patch('openai.OpenAI', return_value=client):
        adapter = AgentDojoAdapter()
        with pytest.raises(openai.APIStatusError):
            client.chat.completions.create()
        with pytest.raises(CallsStopped):
            client.chat.completions.create()
    assert len(calls) == 1
    assert adapter.api_calls[0]['status_code'] == 402
    assert adapter.api_calls[1]['http_requests'] == 0
    assert adapter.api_calls[0]['usage'] is None
    assert adapter.api_calls[0]['cost_usd'] is None
    assert 'PRIVATE_' not in (tmp_path / 'calls.jsonl').read_text()
