"""Permission-boundary tests for jailbreak evidence sampling."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from src.adapters.agentdojo import AgentDojoAdapter
from src.evidence.probe_sampling import (
    DELIVERY_SCOPE, DESCRIPTION, PROTOCOL, public_packets as probe_public_packets,
    validate_channel_config,
)
from src.evaluation.mismatch_online import score_run as legacy_score_run
from src.feedback.schema import PublicAttempt
from src.mismatch.online import live_packets, run_branch, target_job
from src.mismatch.recovery import inspect_run
from src.schema import Payload, TargetResult

ROOT = Path(__file__).resolve().parents[1]
NONCE = 'a' * 16
CONFIG = json.loads((ROOT / 'configs/mismatch/probe_sampling_g11.json').read_text())


def channel_config(nonce=NONCE):
    return validate_channel_config(dict(CONFIG, nonce=nonce))


def probe_packet(nonce=NONCE):
    reports = [dict(kind='file', id='11', state='absent'),
               dict(kind='email_attack', id=CONFIG['address'], state='absent')]
    delivery = dict(delivery_id='delivery_0', sequence=2,
                    received_at='2026-01-01T00:00:00+00:00',
                    sender='target@example.org', recipient=CONFIG['address'],
                    subject='status',
                    body='file 11 is absent; the main attack email with five '
                         'attachments is deleted',
                    accepted=True, reports=reports)
    return probe_public_packets(dict(status='completed', channel_id=CONFIG['channel_id'],
                                     nonce=nonce, episode_id='episode', deliveries=[delivery]))[0]


def replay_packet(index=0):
    return dict(query_id=f'event_{index}', description='receipt',
                scope='returned_call_fields_only',
                response=dict(supported=True, evidence='{"call": {}, "result": [], "error": null}'))


def public_case():
    case = json.loads((ROOT / 'reports/mismatch_p0_20261104/inputs/S01.json').read_text())
    return {key: case[key] for key in (
        'suite_name', 'user_task_id', 'injection_task_id', 'user_task_prompt',
        'injection_task_goal', 'context_with_placeholder', 'injection_candidates')}


def probe_report():
    reports = [dict(kind='file', id='11', state='absent'),
               dict(kind='email_attack', id=CONFIG['address'], state='absent')]
    return dict(protocol=PROTOCOL, status='completed', channel_id=CONFIG['channel_id'],
                address=CONFIG['address'], nonce=NONCE, episode_id='episode',
                queries=[], deliveries=[dict(
                    delivery_id='delivery_0', sequence=2,
                    received_at='2026-01-01T00:00:00+00:00',
                    sender='target@example.org', recipient=CONFIG['address'],
                    subject='status',
                    body='file 11 is absent; the main attack email with five '
                         'attachments is deleted', accepted=True,
                    reports=reports)],
                writes=[], pre_state={}, end_state={}, facts=[],
                diagnostics=dict(delivery_accepted=True))


class ViewPermissionTests(unittest.TestCase):
    def test_view_scopes_are_strict(self):
        probe = PublicAttempt(public_case(), 'strategy', Payload('P0', 'x'), '', 'end',
                              (probe_packet(),), origin='target_execution')
        self.assertEqual(probe.view('final_only')['packets'], [])
        self.assertEqual(len(probe.view('receiver_report_only')['packets']), 1)
        with self.assertRaises(ValueError):
            probe.view('authorized_evidence')
        replay = PublicAttempt(public_case(), 'strategy', Payload('P0', 'x'), '', 'end',
                               (replay_packet(),), origin='target_execution')
        with self.assertRaises(ValueError):
            replay.view('receiver_report_only')
        self.assertEqual(len(replay.view('authorized_evidence')['packets']), 1)

    def test_mixed_scopes_rejected(self):
        with self.assertRaises(ValueError):
            PublicAttempt(public_case(), 'strategy', Payload('P0', 'x'), '', 'end',
                          (probe_packet(), replay_packet(1)), origin='target_execution')

    def test_framework_verification_packets_still_rejected(self):
        packet = replay_packet()
        packet['response']['evidence'] = json.dumps(
            dict(protocol='framework_assisted_target_verification_v2'))
        with self.assertRaises(ValueError):
            PublicAttempt(public_case(), 'strategy', Payload('P0', 'x'), '', 'end',
                          (packet,), origin='target_execution')


class TargetJobPermissionTests(unittest.TestCase):
    def test_probe_public_projection_is_minimal(self):
        result = TargetResult(gold=False, final='Target text.',
                              black_box_final='Target text.\n[tool_call] send_email({"body": '
                                              '"PRIVATE_BODY_SENTINEL"})',
                              raw=dict(messages=[dict(role='assistant', content=[
                                  dict(type='text', content='Target text.')])],
                                  probe_sampling=probe_report()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = dict(case=public_case(), payload=dict(id='P0', text='payload'),
                           target_config=dict(probe_sampling=channel_config()),
                           episode_id='episode', feedback_projection='probe_receiver',
                           final_projection='text_only')
            (root / 'request_public.json').write_text(json.dumps(request))
            with patch('src.mismatch.online.configure'), \
                 patch('src.mismatch.tasks.MismatchTargetAdapter',
                       return_value=SimpleNamespace(execute=lambda *args: result)):
                target_job(root / 'request_public.json')
            private = (root / 'execution_private.json').read_text()
            public = json.loads((root / 'feedback_public.json').read_text())
            self.assertIn('PRIVATE_BODY_SENTINEL', private)
            self.assertEqual(public['final_output'], 'Target text.')
            self.assertEqual(len(public['packets']), 1)
            self.assertEqual(public['packets'][0]['scope'], DELIVERY_SCOPE)
            self.assertNotIn('PRIVATE_BODY_SENTINEL', json.dumps(public))

    def test_live_packets_fails_closed_on_probe_diagnostics(self):
        with self.assertRaises(ValueError):
            live_packets(dict(raw=dict(probe_sampling=probe_report())), 'workspace')

    def test_legacy_branch_runner_rejects_probe_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'job_public.json').write_text(json.dumps(dict(
                target_config=dict(probe_sampling=channel_config()))))
            with patch('src.mismatch.online.configure') as configure:
                with self.assertRaises(ValueError):
                    run_branch(root)
                configure.assert_not_called()

    def test_adapter_rejects_combined_protocols(self):
        with self.assertRaises(ValueError):
            AgentDojoAdapter(pipeline=SimpleNamespace(),
                             active_verification=dict(
                                 protocol='framework_assisted_target_verification_v2',
                                 mode='followup', email_ids=['34'], file_ids=['11']),
                             probe_sampling=channel_config())


class LegacyToolGuardTests(unittest.TestCase):
    def test_recovery_rejects_sampling_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'status.json').write_text(json.dumps(dict(status='completed')))
            (root / 'manifest.json').write_text(json.dumps(dict(protocol=PROTOCOL)))
            with self.assertRaises(ValueError):
                inspect_run(root, False)

    def test_legacy_scorer_rejects_sampling_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'status.json').write_text(json.dumps(dict(status='predictions_frozen')))
            (root / 'manifest.json').write_text(json.dumps(dict(protocol=PROTOCOL)))
            with self.assertRaises(ValueError):
                legacy_score_run(root)


if __name__ == '__main__':
    unittest.main()
