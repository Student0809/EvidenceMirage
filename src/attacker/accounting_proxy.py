"""Loopback Anthropic forwarder recording each upstream request, including retries.

No prompts, response text, URLs, headers or credentials enter the journal.
The upstream base URL is fixed by the parent launcher, never by client input.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import urlsplit
import httpx

from .call_logging import (CallsStopped, begin_call, dispatch_call, finish_call,
                           numeric_tree, stop_on_status)


class UsageCollector:
    def __init__(self, streaming=False):
        self.pending = b''
        self.raw = bytearray()
        self.usage = None
        self.streaming = streaming
        self.final_usage_seen = False
        self.message_stopped = False

    def accept(self, obj):
        if not isinstance(obj, dict):
            return
        if obj.get('type') in ('message_start', 'message_delta', 'message_stop'):
            self.streaming = True
        if obj.get('type') == 'message_stop':
            self.message_stopped = True
        if obj.get('type') == 'message_delta' and isinstance(obj.get('usage'), dict):
            self.final_usage_seen = True
        usage = obj.get('usage')
        if isinstance(usage, dict):
            if self.usage is None:
                self.usage = {}
            self.usage.update(numeric_tree(usage))
        message = obj.get('message')
        if isinstance(message, dict):
            self.accept(message)

    def feed(self, chunk):
        self.raw.extend(chunk)
        self.pending += chunk
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            if line.startswith(b'data: '):
                try:
                    self.accept(json.loads(line[6:]))
                except ValueError:
                    pass

    def finish(self):
        # Non-streaming JSON is a single object. SSE may end without a newline.
        data = self.pending[6:] if self.pending.startswith(b'data: ') else bytes(self.raw)
        try:
            self.accept(json.loads(data))
        except ValueError:
            pass
        return self.usage

    @property
    def complete(self):
        return (self.usage is not None and
                (not self.streaming or (self.final_usage_seen and self.message_stopped)))


class AccountingProxy:
    def __init__(self, upstream, timeout=900):
        self.upstream = upstream.rstrip('/')
        self.timeout = timeout
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *args):
                pass

            def do_POST(self):
                record = dict(status='running', stage=self.headers.get('X-PIMiner-Stage', 'unknown'),
                              parent_call_id=self.headers.get('X-PIMiner-Call-Id'))
                started, collector = time.monotonic(), UsageCollector()
                response_started = False
                try:
                    body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                    request = None
                    try:
                        request = json.loads(body)
                        record['model'] = request.get('model')
                    except (ValueError, AttributeError):
                        record['model'] = None
                    begin_call(record, 'attacker_http')
                    # Only messages/token-count paths used by this CLI may pass.
                    if urlsplit(self.path).path not in ('/v1/messages', '/v1/messages/count_tokens') or self.path.startswith('//'):
                        raise ValueError('Unexpected Anthropic request path')
                    headers = {k: v for k, v in self.headers.items()
                               if k.lower() not in ('host', 'connection', 'content-length',
                                                    'transfer-encoding', 'accept-encoding')
                               and not k.lower().startswith('x-piminer-')}
                    dispatch_call(record)
                    with httpx.Client(timeout=owner.timeout, follow_redirects=False, trust_env=False) as client:
                        with client.stream('POST', owner.upstream + self.path,
                                           content=body, headers=headers) as response:
                            collector.streaming = ('text/event-stream' in response.headers.get('content-type', '')
                                                   or (isinstance(request, dict) and request.get('stream') is True))
                            record['status_code'] = response.status_code
                            stop_on_status(record)
                            self.send_response(response.status_code)
                            for key in ('content-type', 'retry-after', 'request-id'):
                                if key in response.headers:
                                    self.send_header(key, response.headers[key])
                            self.send_header('Connection', 'close')
                            self.end_headers()
                            response_started = True
                            for chunk in response.iter_bytes():
                                collector.feed(chunk)
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            record['status'] = 'completed' if response.is_success else 'error'
                except Exception as exc:
                    record.update(status='blocked' if isinstance(exc, CallsStopped) else 'error',
                                  error_type=type(exc).__name__)
                    if not response_started:
                        try:
                            self.send_response(402 if isinstance(exc, CallsStopped) else 502)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            self.wfile.write(b'{"type":"error","error":{"type":"api_error","message":"Local accounting transport stopped"}}')
                        except OSError:
                            pass
                finally:
                    record.update(seconds=time.monotonic()-started, usage=collector.finish())
                    record['usage_complete'] = collector.complete
                    if record.get('status') == 'completed' and collector.streaming and not collector.message_stopped:
                        record.update(status='error', error_type='IncompleteProviderStream')
                    finish_call(record)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
