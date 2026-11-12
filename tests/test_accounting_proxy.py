import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from unittest.mock import patch

import httpx
import pytest

from src.attacker.accounting_proxy import AccountingProxy, UsageCollector


def test_usage_parser_keeps_cumulative_stream_output_and_partial_input():
    c = UsageCollector()
    content = b'event: message_start\ndata: {"message":{"usage":{"input_tokens":9,"output_tokens":1}}}\n\nevent: message_delta\ndata: {"usage":{"output_tokens":5}}\n\n'
    for i in range(0, len(content), 7):
        c.feed(content[i:i+7])
    assert c.finish() == dict(input_tokens=9, output_tokens=5)
    c = UsageCollector()
    c.feed(json.dumps(dict(usage=dict(input_tokens=12, output_tokens=3)), indent=2).encode())
    assert c.finish() == dict(input_tokens=12, output_tokens=3)


def test_incomplete_stream_preserves_partial_usage_but_never_claims_complete():
    c = UsageCollector()
    c.feed(b'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}\n\n')
    assert c.finish() == dict(input_tokens=10, output_tokens=0)
    assert c.complete is False
    c.feed(b'data: {"type":"message_delta","usage":{"output_tokens":4}}\n\n')
    assert c.complete is False
    c.feed(b'data: {"type":"message_stop"}\n\n')
    assert c.finish()['output_tokens'] == 4
    assert c.complete is True


def test_unnamed_error_before_message_start_is_an_incomplete_stream():
    c = UsageCollector(streaming=True)
    c.feed(b'data: {"error":{"message":"provider wait timeout"}}\n\n')
    assert c.finish() is None
    assert c.streaming and not c.message_stopped and not c.complete


@pytest.mark.parametrize('status', [200, 402, 429])
def test_forwarder_logs_requests_without_secrets_and_stops_on_402(tmp_path, status):
    upstream_calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            upstream_calls.append(dict(headers=dict(self.headers), body=body))
            payload = json.dumps(dict(usage=dict(input_tokens=8, output_tokens=4),
                                     content='PRIVATE_RESPONSE')).encode()
            self.send_response(status)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(payload)))
            self.send_header('retry-after', '1')
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    log = tmp_path / 'calls.jsonl'
    try:
        with patch.dict(os.environ, PIMINER_CALL_LOG=str(log), PIMINER_STOP_FILE=str(tmp_path/'stop')):
            proxy = AccountingProxy(f'http://127.0.0.1:{upstream.server_port}')
            endpoint = proxy.start()
            try:
                for _ in range(2):
                    response = httpx.post(endpoint+'/v1/messages?beta=true',
                        headers={'Authorization':'Bearer PRIVATE_TOKEN', 'X-PIMiner-Call-Id':'parent-fixture'},
                        json={'model':'fixture','messages':['PRIVATE_PROMPT']}, trust_env=False)
                    assert response.status_code == status
                for _ in range(50):
                    rows = [json.loads(line) for line in log.read_text().splitlines()]
                    if sum(row['event']=='finish' for row in rows)==2:
                        break
                    time.sleep(.01)
            finally:
                proxy.close()
    finally:
        upstream.shutdown(); upstream.server_close(); thread.join()
    assert len(upstream_calls) == (1 if status == 402 else 2)
    assert upstream_calls[0]['headers']['Authorization'] == 'Bearer PRIVATE_TOKEN'
    assert not any(k.lower().startswith('x-piminer') for k in upstream_calls[0]['headers'])
    assert 'PRIVATE_' not in log.read_text()
    finished = [row for row in rows if row['event']=='finish']
    assert finished[0]['usage'] == dict(input_tokens=8, output_tokens=4)
    assert finished[0]['parent_call_id'] == 'parent-fixture'
    assert finished[0]['http_requests'] == 1
    assert finished[0]['cost_usd'] is None
