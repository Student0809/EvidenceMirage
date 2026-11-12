"""Exercise the installed Claude binary against a loopback error-only provider.

No real credentials are inherited. Only synthetic prompts are sent to localhost.
Counts the real requests reaching this local endpoint, including CLI retries.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time


def probe(status):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            requests.append(dict(seconds=time.monotonic() - started, path=self.path))
            payload = json.dumps(dict(type='error', error=dict(
                type='rate_limit_error' if status == 429 else 'invalid_request_error',
                message='offline rate limit' if status == 429 else 'Insufficient Balance'))).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('retry-after', '1')
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    started = time.monotonic()
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='asr-cli-offline-') as directory:
            env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LD_LIBRARY_PATH') if key in os.environ}
            env.update(ANTHROPIC_BASE_URL=f'http://127.0.0.1:{server.server_port}',
                       ANTHROPIC_AUTH_TOKEN='offline-dummy-not-a-real-key',
                       CLAUDE_CONFIG_DIR=directory, CLAUDE_CODE_MAX_RETRIES='2',
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                       CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES='0')
            command = ['claude', '--bare', '-p', '--model', 'deepseek-flash[1m]', '--effort', 'max',
                       '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                       '--setting-sources', '', '--no-session-persistence', '--max-turns', '1',
                       '--output-format', 'json', '--system-prompt', 'Offline synthetic transport test.']
            result = subprocess.run(command, input='Reply OK.', cwd=directory, env=env,
                                    text=True, capture_output=True, timeout=55)
            try:
                envelope = json.loads(result.stdout)
            except ValueError:
                envelope = dict(parse_failed=True)
            return dict(status_code=status, local_http_requests=requests,
                        local_http_count=len(requests), paid_calls=0, returncode=result.returncode,
                        seconds=time.monotonic()-started, envelope=envelope,
                        stderr=result.stderr[:2000], configured_max_retries=2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', type=int, choices=[402, 429], required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    path = Path(args.output)
    if path.exists():
        raise FileExistsError(path)
    result = probe(args.status)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({k:v for k,v in result.items() if k not in ('envelope','stderr')}))
