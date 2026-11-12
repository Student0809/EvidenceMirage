"""A Target-side read is not an attacker-visible observation channel."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from src.mismatch.online import live_packets, target_job, run_branch
from src.evidence.active_verification import public_packets, validate_config
from src.feedback.schema import public_packets as validate_packets
from src.mismatch.recovery import inspect_run
from src.schema import TargetResult


class VerificationPermissionTests(unittest.TestCase):
    def test_internal_reads_never_become_attacker_packets(self):
        original = dict(raw=dict(messages=[]))
        with_private_read = deepcopy(original)
        with_private_read['raw']['active_verification'] = dict(
            protocol='active_tool_mediated_verification_v1',
            final='PRIVATE_VERIFICATION_FINAL',
            facts=[dict(kind='file', object_id='11', state='absent',
                        scope='current_cloud_drive', query_id='verification_0')],
            receipts=[dict(query_id='verification_0', call=dict(function='list_files', args={}),
                           result=[], error=None)])
        self.assertEqual(live_packets(with_private_read, 'workspace'),
                         live_packets(original, 'workspace'))
        self.assertEqual(public_packets(with_private_read['raw']['active_verification']), [])

    def test_legacy_packets_cannot_enter_controller_history(self):
        for protocol in ('active_tool_mediated_verification_v1', 'framework_assisted_target_verification_v2'):
            packet = dict(query_id='event_0', description='internal read', scope='returned_call_fields_only',
                          response=dict(supported=True, evidence=json.dumps(dict(protocol=protocol,
                              state='absent', object_id='11'))))
            with self.assertRaisesRegex(ValueError, 'not attacker-visible'):
                validate_packets([packet])

    def test_legacy_jobs_and_recovery_fail_before_dispatch(self):
        legacy = dict(mode='followup', email_ids=['34'])
        with self.assertRaisesRegex(ValueError, 'Explicit framework'):
            validate_config(legacy)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'job_public.json').write_text(json.dumps(dict(target_config=dict(active_verification=legacy))))
            with patch('src.mismatch.online.configure') as configure:
                with self.assertRaisesRegex(ValueError, 'Explicit framework'):
                    run_branch(root)
                configure.assert_not_called()
            (root/'status.json').write_text(json.dumps(dict(status='completed_with_errors')))
            (root/'manifest.json').write_text(json.dumps(dict(target=dict(active_verification=legacy))))
            with self.assertRaisesRegex(ValueError, 'Explicit framework'):
                inspect_run(root, False)

    def test_target_job_keeps_verification_final_and_facts_in_private_artifact_only(self):
        result = TargetResult(gold=False, final='original report', black_box_final='original report', raw=dict(
            messages=[], active_verification=dict(final='PRIVATE_VERIFICATION_SENTINEL',
                facts=[dict(state='absent')], receipts=[dict(result='PRIVATE_RECEIPT_SENTINEL')]),
            post='PRIVATE_STATE_SENTINEL'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = root/'request_public.json'
            request.write_text(json.dumps(dict(target_config={}, case=dict(suite_name='workspace'),
                                               payload=dict(id='P0', text='fixture'), episode_id='fixture')))
            with patch('src.mismatch.online.configure'), patch('src.mismatch.tasks.MismatchTargetAdapter',
                    return_value=SimpleNamespace(execute=lambda *args: result)), patch.dict('os.environ'):
                target_job(request)
            private = (root/'execution_private.json').read_text()
            public = (root/'feedback_public.json').read_text()
            self.assertIn('PRIVATE_VERIFICATION_SENTINEL', private)
            self.assertNotIn('PRIVATE_', public)
            self.assertEqual(json.loads(public)['packets'], [])
            self.assertEqual(json.loads(public)['final_output'], 'original report')


if __name__ == '__main__':
    unittest.main()
