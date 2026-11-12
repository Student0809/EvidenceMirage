from .call_logging import begin_call, dispatch_call, capture_cli, finish_call, cli_environment
"""Tool-free Claude CLI transport with an explicit, recorded role prompt."""
import json
import os
import signal
import subprocess
import tempfile
import time


class RoleDecision:
    def __init__(self, model, effort, timeout, system_prompt):
        self.model, self.effort, self.timeout = model, effort, timeout
        self.system_prompt = system_prompt
        self.calls = []

    def __call__(self, prompt, stage):
        record = dict(stage=stage, model=self.model, effort=self.effort,
                      system_prompt=self.system_prompt, status='running')
        self.calls.append(record)
        start = time.monotonic()
        try:
            begin_call(record, 'attacker_cli')
            with tempfile.TemporaryDirectory(prefix='pim-original-') as directory:
                command = ['claude', '--bare', '-p', '--model', self.model, '--effort', self.effort,
                           '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                           '--setting-sources', '', '--no-session-persistence', '--max-turns', '1',
                           '--output-format', 'json', '--system-prompt', self.system_prompt]
                dispatch_call(record)
                process = subprocess.Popen(command, cwd=directory, env=cli_environment(record), stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, start_new_session=True)
                try:
                    output, stderr = process.communicate(prompt, timeout=self.timeout)
                finally:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.communicate()
                capture_cli(record, output, stderr, process.returncode)
                if process.returncode:
                    raise RuntimeError('PIMiner CLI failed')
                result = json.loads(output)
                pass  # Usage was captured before exit/result validation.
                if result.get('is_error') or not isinstance(result.get('result'), str):
                    raise RuntimeError('PIMiner CLI returned an error or missing result')
                record['status'] = 'completed'
                return result['result']
        except Exception as exc:
            record.update(status='error', error_type=type(exc).__name__)
            raise
        finally:
            record['seconds'] = time.monotonic() - start
            finish_call(record)
