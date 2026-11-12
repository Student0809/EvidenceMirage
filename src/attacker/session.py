from .call_logging import begin_call, dispatch_call, capture_cli, finish_call, cli_environment
"""A tool-free, persistent Claude conversation owned by one runner branch.

Each turn uses a new CLI process but resumes the same saved conversation. A
failed or lost turn poisons the transport: its context cannot safely be retried.
"""
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid


class PersistentRoleDecision:
    def __init__(self, model, effort, timeout, system_prompt, session_dir):
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('Session timeout must be finite and positive')
        self.model, self.effort, self.timeout = model, effort, timeout
        self.system_prompt = system_prompt
        self.session_dir = Path(session_dir).resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        if any(self.session_dir.iterdir()):
            raise ValueError('Persistent session directory must be empty; never restart an existing branch')
        self.config_dir = self.session_dir / 'claude-config'
        self.config_dir.mkdir()
        self.session_id = str(uuid.uuid4())
        self.started = False
        self.failed = False
        self.calls = []
        self._save_state()

    def _save_state(self):
        # Transport metadata contains no prompts, outputs, environment or secrets.
        path = self.session_dir / 'transport.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(session_id=self.session_id, started=self.started,
                                            failed=self.failed, calls=self.calls), indent=2) + '\n')
        temporary.replace(path)

    def _check_transcript(self):
        # The local CLI persists <UUID>.jsonl under its config directory. Do not
        # depend on its project-directory escaping or inspect a global profile.
        paths = list(self.config_dir.rglob(self.session_id + '.jsonl'))
        if len(paths) != 1 or not paths[0].is_file() or paths[0].stat().st_size == 0:
            raise RuntimeError('Persistent conversation transcript is missing or ambiguous')
        return paths[0]

    def __call__(self, prompt, stage):
        record = dict(stage=stage, model=self.model, effort=self.effort,
                      session_id=self.session_id, resumed=self.started,
                      status='running', usage=None, model_usage=None)
        self.calls.append(record)
        start = time.monotonic()
        try:
            begin_call(record, 'attacker_cli')
            if self.failed:
                raise RuntimeError('Persistent conversation failed; branch cannot be continued')
            if self.started:
                self._check_transcript()
            self._save_state()
            command = ['claude', '--bare', '-p', '--model', self.model, '--effort', self.effort,
                       '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                       '--setting-sources', '', '--max-turns', '1', '--output-format', 'json',
                       '--system-prompt', self.system_prompt,
                       '--resume' if self.started else '--session-id', self.session_id]
            environment = cli_environment(record, CLAUDE_CONFIG_DIR=str(self.config_dir))
            dispatch_call(record)
            process = subprocess.Popen(command, cwd=str(self.session_dir), env=environment,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
                raise RuntimeError('Persistent CLI failed')
            result = json.loads(output)
            if not isinstance(result, dict):
                raise RuntimeError('Persistent CLI returned a non-object result')
            pass  # Usage was captured before exit/result validation.
            if result.get('session_id') != self.session_id:
                raise RuntimeError('Persistent CLI returned a missing or different session ID')
            if result.get('is_error') or not isinstance(result.get('result'), str):
                raise RuntimeError('Persistent CLI returned an error or missing result')
            self._check_transcript()
            self.started = True
            record['status'] = 'completed'
            return result['result']
        except BaseException as exc:
            self.failed = True
            record.update(status='error', error_type=type(exc).__name__)
            raise
        finally:
            record['seconds'] = time.monotonic() - start
            finish_call(record)
            try:
                self._save_state()
            except BaseException as exc:
                self.failed = True
                record.update(status='error', error_type=type(exc).__name__)
                raise
