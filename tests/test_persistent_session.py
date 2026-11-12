"""Persistent-conversation transport witnesses; no remote model calls."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import Mock, patch
import uuid

from src.attacker.session import PersistentRoleDecision


class PersistentSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.decision = PersistentRoleDecision('fixture-model', 'max', 7, 'private-role', self.root / 'session')

    def transcript(self):
        path = self.decision.config_dir / 'projects' / 'fixture' / (self.decision.session_id + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'type': 'assistant', 'sessionId': self.decision.session_id}) + '\n')
        return path

    def process(self, result=None, *, persist=True, returncode=0):
        if result is None:
            result = dict(result='answer', session_id=self.decision.session_id,
                          usage={'input_tokens': 7}, modelUsage={'fixture-model': {'outputTokens': 3}})
        def communicate(*args, **kwargs):
            if persist:
                self.transcript()
            return json.dumps(result), 'private-stderr'
        return types.SimpleNamespace(returncode=returncode, communicate=communicate, poll=lambda: returncode)

    def assert_poisoned(self, expected_error):
        self.assertTrue(self.decision.failed)
        self.assertEqual(self.decision.calls[-1]['error_type'], expected_error)
        with patch('src.attacker.session.subprocess.Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'cannot be continued'):
                self.decision('never retry this context', 'later')
            popen.assert_not_called()
        state = json.loads((self.decision.session_dir / 'transport.json').read_text())
        self.assertTrue(state['failed'])
        self.assertEqual(state['calls'][-1]['status'], 'error')

    def test_first_turn_and_resume_share_context_identity_but_keep_tools_disabled(self):
        self.assertEqual(str(uuid.UUID(self.decision.session_id)), self.decision.session_id)
        self.assertFalse(self.decision.started)
        with patch.dict(os.environ, {'ANTHROPIC_AUTH_TOKEN': 'private-credential'}), \
                patch('src.attacker.session.subprocess.Popen', side_effect=lambda *a, **k: self.process()) as popen:
            self.assertEqual(self.decision('first-private-prompt', 'initial'), 'answer')
            self.assertTrue(self.decision.started)
            self.assertEqual(self.decision('second-private-prompt', 'revise'), 'answer')
        self.assertEqual(popen.call_count, 2)
        first, second = [c.args[0] for c in popen.call_args_list]
        self.assertEqual(first[first.index('--session-id') + 1], self.decision.session_id)
        self.assertNotIn('--resume', first)
        self.assertEqual(second[second.index('--resume') + 1], self.decision.session_id)
        self.assertNotIn('--session-id', second)
        for call in popen.call_args_list:
            command = call.args[0]
            self.assertEqual(command[command.index('--tools') + 1], '')
            self.assertEqual(command[command.index('--max-turns') + 1], '1')
            self.assertEqual(command[command.index('--mcp-config') + 1], '{"mcpServers":{}}')
            self.assertEqual(command[command.index('--setting-sources') + 1], '')
            self.assertIn('--bare', command)
            self.assertIn('--strict-mcp-config', command)
            self.assertNotIn('--no-session-persistence', command)
            self.assertNotIn('--fork-session', command)
            self.assertNotIn('--continue', command)
            self.assertNotIn('--dangerously-skip-permissions', command)
            self.assertEqual(call.kwargs['cwd'], str(self.decision.session_dir))
            self.assertEqual(call.kwargs['env']['CLAUDE_CONFIG_DIR'], str(self.decision.config_dir))
            self.assertEqual(call.kwargs['env']['ANTHROPIC_AUTH_TOKEN'], 'private-credential')
            self.assertTrue(call.kwargs['start_new_session'])
        self.assertEqual([c['resumed'] for c in self.decision.calls], [False, True])
        self.assertEqual(self.decision.calls[-1]['usage'], {'input_tokens': 7})
        self.assertEqual(self.decision.calls[-1]['model_usage'], {'fixture-model': {'outputTokens': 3}})
        self.assertTrue(all(c['seconds'] >= 0 for c in self.decision.calls))
        saved = (self.decision.session_dir / 'transport.json').read_text()
        for private in ['private-role', 'first-private-prompt', 'second-private-prompt', 'private-credential', 'private-stderr']:
            self.assertNotIn(private, saved)

    def test_existing_branch_directory_cannot_silently_start_another_conversation(self):
        with self.assertRaisesRegex(ValueError, 'never restart'):
            PersistentRoleDecision('fixture', 'max', 1, '', self.decision.session_dir)
        # Parent launchers may precreate the otherwise empty directory.
        empty = self.root / 'empty'
        empty.mkdir()
        other = PersistentRoleDecision('fixture', 'max', 1, '', empty)
        self.assertNotEqual(other.session_id, self.decision.session_id)

    def test_invalid_timeouts_cannot_create_unbounded_sessions(self):
        for timeout in [0, -1, float('inf'), float('nan'), True, None]:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                PersistentRoleDecision('fixture', 'max', timeout, '', self.root / 'invalid')
        self.assertFalse((self.root / 'invalid').exists())

    def test_missing_resume_transcript_fails_before_invoking_cli(self):
        with patch('src.attacker.session.subprocess.Popen', return_value=self.process()):
            self.decision('first', 'initial')
        self.transcript().unlink()
        with patch('src.attacker.session.subprocess.Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'transcript'):
                self.decision('second', 'revise')
            popen.assert_not_called()
        self.assert_poisoned('RuntimeError')

    def test_success_without_persisted_transcript_cannot_start_or_resume(self):
        with patch('src.attacker.session.subprocess.Popen', return_value=self.process(persist=False)):
            with self.assertRaisesRegex(RuntimeError, 'transcript'):
                self.decision('first', 'initial')
        self.assertFalse(self.decision.started)
        self.assert_poisoned('RuntimeError')

    def test_missing_or_changed_result_session_id_never_becomes_a_fresh_session(self):
        for resumed in [False, True]:
            for session_id in [None, str(uuid.uuid4())]:
                with self.subTest(resumed=resumed, session_id=session_id):
                    self.decision = PersistentRoleDecision('fixture', 'max', 7, '', self.root / str(uuid.uuid4()))
                    if resumed:
                        with patch('src.attacker.session.subprocess.Popen', return_value=self.process()):
                            self.decision('first', 'initial')
                    result = dict(result='looks successful', session_id=session_id)
                    with patch('src.attacker.session.subprocess.Popen', return_value=self.process(result)) as popen:
                        with self.assertRaisesRegex(RuntimeError, 'session ID'):
                            self.decision('next', 'revise')
                        self.assertEqual(popen.call_count, 1)
                    self.assertEqual(self.decision.started, resumed)
                    self.assert_poisoned('RuntimeError')

    def test_cli_error_or_invalid_result_poison_the_branch_without_retry(self):
        variants = [({}, 1), ({'is_error': True, 'result': 'hidden error'}, 0),
                    ({'result': None}, 0), ([], 0)]
        for result, code in variants:
            with self.subTest(result=result, code=code):
                self.decision = PersistentRoleDecision('fixture', 'max', 7, '', self.root / str(uuid.uuid4()))
                if isinstance(result, dict):
                    result = dict(result, session_id=self.decision.session_id)
                with patch('src.attacker.session.subprocess.Popen', return_value=self.process(result, returncode=code)) as popen:
                    with self.assertRaises(RuntimeError):
                        self.decision('first', 'initial')
                    self.assertEqual(popen.call_count, 1)
                self.assert_poisoned('RuntimeError')

    def test_launch_and_json_errors_are_recorded_without_exposing_output(self):
        for failure in ['launch', 'parse']:
            with self.subTest(failure=failure):
                self.decision = PersistentRoleDecision('fixture', 'max', 7, '', self.root / str(uuid.uuid4()))
                process = types.SimpleNamespace(returncode=0, poll=lambda: 0,
                                               communicate=lambda *a, **k: ('private-malformed-json', 'private-error'))
                with patch('src.attacker.session.subprocess.Popen', side_effect=OSError('private-path') if failure == 'launch' else None,
                           return_value=process):
                    with self.assertRaises(OSError if failure == 'launch' else json.JSONDecodeError):
                        self.decision('private-prompt', 'initial')
                self.assert_poisoned('OSError' if failure == 'launch' else 'JSONDecodeError')
                self.assertNotIn('private-', (self.decision.session_dir / 'transport.json').read_text())

    def test_timeout_kills_entire_process_group_and_never_retries(self):
        process = types.SimpleNamespace(returncode=None, pid=712345)
        process.poll = lambda: process.returncode
        process.communicate = Mock(side_effect=[subprocess.TimeoutExpired('claude', 7), ('', '')])
        def killed(pid, signum):
            self.assertEqual((pid, signum), (process.pid, signal.SIGKILL))
            process.returncode = -signal.SIGKILL
        with patch('src.attacker.session.subprocess.Popen', return_value=process) as popen, \
                patch('src.attacker.session.os.killpg', side_effect=killed) as killpg:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.decision('first', 'initial')
            popen.assert_called_once()
            killpg.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)
        self.assertFalse(self.decision.started)
        self.assert_poisoned('TimeoutExpired')

    def test_interrupted_turn_is_poisoned_and_reaps_child(self):
        process = types.SimpleNamespace(returncode=None, pid=712345)
        process.poll = lambda: process.returncode
        process.communicate = Mock(side_effect=[KeyboardInterrupt(), ('', '')])
        with patch('src.attacker.session.subprocess.Popen', return_value=process), \
                patch('src.attacker.session.os.killpg') as killpg:
            with self.assertRaises(KeyboardInterrupt):
                self.decision('first', 'initial')
            killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)
        self.assert_poisoned('KeyboardInterrupt')

    def test_stateful_fake_cli_recalls_unrepeated_context_in_a_new_process(self):
        binary = self.root / 'bin'
        binary.mkdir()
        executable = binary / 'claude'
        executable.write_text('#!' + sys.executable + '\n' + textwrap.dedent('''\
            import json, os, pathlib, sys
            args = sys.argv[1:]
            resumed = '--resume' in args
            flag = '--resume' if resumed else '--session-id'
            session_id = args[args.index(flag) + 1]
            path = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects' / 'fake' / (session_id + '.jsonl')
            text = sys.stdin.read()
            if resumed:
                if not path.exists():
                    sys.exit(23)
                history = [json.loads(line) for line in path.read_text().splitlines()]
                answer = history[0]['remember'] if text == 'recall' else 'unsupported'
            else:
                if path.exists() or not text.startswith('remember:'):
                    sys.exit(24)
                path.parent.mkdir(parents=True, exist_ok=True)
                history = [{'remember': text.split(':', 1)[1]}]
                answer = 'stored'
            history.append({'pid': os.getpid(), 'resumed': resumed})
            path.write_text(''.join(json.dumps(row) + '\\n' for row in history))
            print(json.dumps({'result': answer, 'session_id': session_id, 'usage': {'input_tokens': 1}}))
            '''))
        executable.chmod(0o755)
        nonce = str(uuid.uuid4())
        with patch.dict(os.environ, {'PATH': str(binary) + os.pathsep + os.environ.get('PATH', '')}):
            self.assertEqual(self.decision('remember:' + nonce, 'initial'), 'stored')
            self.assertEqual(self.decision('recall', 'revise'), nonce)
        rows = [json.loads(line) for line in self.decision._check_transcript().read_text().splitlines()]
        turns = [row for row in rows if 'pid' in row]
        self.assertEqual([row['resumed'] for row in turns], [False, True])
        self.assertNotEqual(turns[0]['pid'], turns[1]['pid'])
        self.assertEqual([row['session_id'] for row in self.decision.calls], [self.decision.session_id] * 2)


if __name__ == '__main__':
    unittest.main()
