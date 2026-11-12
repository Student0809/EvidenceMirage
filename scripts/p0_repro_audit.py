"""Passive, process-local HTTP and pipeline logging for P0 stability trials."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time


def now():
    return datetime.now(timezone.utc).isoformat()


def plain(value):
    if hasattr(value, 'model_dump'):
        return plain(value.model_dump())
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def safe(value):
    """Never persist configured credentials, including in echoed error content."""
    sensitive = ('api_key', 'apikey', 'authorization', 'access_token', 'auth_token', 'password', 'secret')
    secrets = [v for k, v in os.environ.items()
               if any(s in k.lower() for s in sensitive) and len(v) >= 8]

    def clean(v):
        if isinstance(v, dict):
            return {k: '[REDACTED]' if any(s in k.lower().replace('-', '_') for s in sensitive)
                    else clean(x) for k, x in v.items()}
        if isinstance(v, list):
            return [clean(x) for x in v]
        if isinstance(v, str):
            for secret in secrets:
                v = v.replace(secret, '[REDACTED]')
        return v
    return clean(plain(value))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w') as handle:
        handle.write(json.dumps(safe(value), ensure_ascii=False, indent=2) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_json(path, value):
    with Path(path).open('a') as handle:
        handle.write(json.dumps(safe(value), ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


class HTTPAudit:
    """Wrap Client.send below SDK retries; never issue a request itself.

    Every send gets a durable start row before delegation and a terminal row
    afterward. Keep full JSON bodies, no headers or URL query parameters.
    """
    def __init__(self, output_dir, endpoint='https://api2.aigcbest.top/v1'):
        import httpx
        self.path = Path(output_dir) / 'http_events.jsonl'
        self.endpoint = httpx.URL(endpoint)
        self.records = []
        self.transport_records = []

    def __enter__(self):
        import httpx
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=False)
        self.original = httpx.Client.send
        self.original_single = httpx.Client._send_single_request
        self.transport_path = self.path.with_name('transport_events.jsonl')
        self.transport_path.touch(exist_ok=False)
        audit = self

        def send(client, request, *args, **kwargs):
            url = request.url
            if (url.scheme, url.host, url.port) != (audit.endpoint.scheme, audit.endpoint.host, audit.endpoint.port):
                raise ValueError('HTTP destination differs from the frozen Target endpoint')
            if request.method != 'POST' or url.path != audit.endpoint.path.rstrip('/') + '/chat/completions':
                raise ValueError('Only the frozen Target chat completion route is allowed')
            raw = request.content
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ValueError('Expected a JSON Target request') from None
            index = len(audit.records)
            record = dict(request_index=index, status='started', started_at=now(),
                          method=request.method, path=url.path,
                          sdk_retry_count=request.headers.get('x-stainless-retry-count'),
                          body_sha256=hashlib.sha256(raw).hexdigest(), request=body)
            audit.records.append(record)
            append_json(audit.path, record)
            start = time.monotonic()
            try:
                response = audit.original(client, request, *args, **kwargs)
                record.update(status='response', status_code=response.status_code,
                              redirect_count=len(response.history), request_id=response.headers.get('x-request-id'),
                              completed_at=now(), seconds=time.monotonic()-start)
                # Non-streaming calls only; never consume a stream ourselves.
                if response.is_stream_consumed:
                    try:
                        record['response'] = response.json()
                    except (ValueError, UnicodeError):
                        record['response_body_sha256'] = hashlib.sha256(response.content).hexdigest()
                append_json(audit.path, {k: v for k, v in record.items() if k != 'request'})
                return response
            except BaseException as exc:
                record.update(status='error', error_type=type(exc).__name__, completed_at=now(),
                              seconds=time.monotonic()-start)
                append_json(audit.path, {k: v for k, v in record.items() if k != 'request'})
                raise
        def single(client, request):
            # Also count each transport dispatch, including redirects under one send.
            url = request.url
            if ((url.scheme, url.host, url.port) != (audit.endpoint.scheme, audit.endpoint.host, audit.endpoint.port)
                    or request.method != 'POST' or url.path != audit.endpoint.path.rstrip('/') + '/chat/completions'):
                raise ValueError('Transport destination differs from frozen Target route')
            item = dict(transport_index=len(audit.transport_records), status='started', started_at=now(),
                        send_index=len(audit.records)-1, sdk_retry_count=request.headers.get('x-stainless-retry-count'))
            try:
                item['request'] = json.loads(request.content)
            except httpx.RequestNotRead:
                # Redirects can replay an unread stream. Never consume it for logging.
                item['request_body_unavailable'] = 'unread_stream'
            except (ValueError, UnicodeError):
                item['request_body_unavailable'] = 'non_json'
            audit.transport_records.append(item)
            append_json(audit.transport_path, item)
            try:
                response = audit.original_single(client, request)
                item.update(status='response', completed_at=now(), status_code=response.status_code,
                            request_id=response.headers.get('x-request-id'))
                append_json(audit.transport_path, {k:v for k,v in item.items() if k!='request'})
                return response
            except BaseException as exc:
                item.update(status='error', completed_at=now(), error_type=type(exc).__name__)
                append_json(audit.transport_path, {k:v for k,v in item.items() if k!='request'})
                raise
        httpx.Client.send = send
        httpx.Client._send_single_request = single
        return self

    def __exit__(self, *args):
        import httpx
        httpx.Client.send = self.original
        httpx.Client._send_single_request = self.original_single

    def summary(self):
        return dict(http_send_attempts=len(self.records), transport_attempts=len(self.transport_records),
                    http_responses=sum(r['status']=='response' for r in self.records),
                    http_2xx=sum(200 <= r.get('status_code', 0) < 300 for r in self.records),
                    http_errors=sum(r['status']=='error' or r.get('status_code', 0)>=400 for r in self.records),
                    response_models=sorted({r.get('response', {}).get('model') for r in self.records
                                            if isinstance(r.get('response'), dict) and r['response'].get('model')}))


@contextmanager
def record_pipeline(pipeline, output_dir):
    """Log actual queries without changing return values or exceptions."""
    from agentdojo.logging import LOGGER_STACK
    class Capture:
        messages = []
        def log(self, messages, **kwargs):
            self.messages = deepcopy(messages)
        def log_error(self, *args, **kwargs):
            pass
    original = pipeline.query
    records = []
    path = Path(output_dir) / 'pipeline_queries.jsonl'

    def query(*args, **kwargs):
        capture = Capture()
        token = LOGGER_STACK.set([*LOGGER_STACK.get(), capture])
        env = args[2] if len(args)>2 else kwargs.get('env')
        item = dict(query_index=len(records), status='started', started_at=now(), pre=plain(env))
        records.append(item)
        append_json(path, item)
        try:
            result = original(*args, **kwargs)
            item.update(status='completed', completed_at=now(), post=plain(result[2]), messages=plain(result[3]))
            append_json(path, item)
            return result
        except BaseException as exc:
            item.update(status='error', completed_at=now(), error_type=type(exc).__name__,
                        post=plain(getattr(exc, 'task_environment', env)),
                        messages=plain(getattr(exc, 'messages', capture.messages)),
                        messages_source='exception' if hasattr(exc, 'messages') else 'last_native_log')
            append_json(path, item)
            raise
        finally:
            LOGGER_STACK.reset(token)
    pipeline.query = query
    try:
        yield records
    finally:
        pipeline.query = original
