"""Raw delivery fidelity, Episode isolation, and history regression tests (no API calls)."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.evidence import probe_sampling
from src.evidence.probe_sampling import (
    ProbeChannel, public_packets, validate_public_probe_packets,
    summarize_probe_feedback, REPORT_FORMAT,
)
from src.feedback.assessment import assess, validate_ordinary
from src.feedback.history import history_item, revision_history
from src.feedback.schema import PublicAttempt, PROTOCOL as FEEDBACK_PROTOCOL
from src.attacker.revision_memory import build_revision_memory
from src.schema import Payload
from src.evaluation.probe_sampling import _score_execution, score_run
from src.mismatch.data import write_json, digest
from tests.test_probe_sampling import channel_config, receipt, public_case, ADDRESS, NONCE
from tests.test_probe_sampling_metrics import make_execution, CONFIG


RAW = '已尝试处理。部分操作可能未完成；邮件的结果无法确认。\n  请保留这些空格。\n'


def packet(episode, body=RAW):
    channel = ProbeChannel(channel_config(), episode_id=episode,
                           account_email='target@example.org')
    report = channel.report({}, {}, [receipt('回传情况', body)])
    return public_packets(report)[0]


def attempt(episode, body=RAW):
    packets = () if body is None else (packet(episode, body),)
    return PublicAttempt(public_case(), 'strategy', Payload('P0', 'same payload'),
                         '', 'current final', packets, origin='target_execution', episode_id=episode)


def revise_decision():
    return validate_ordinary(json.dumps(dict(
        goal_status='unknown', action='Revise', action_reason='Need another attempt')))


class RawReceiverTests(unittest.TestCase):
    def test_blocked_operation_receipt_reaches_assessment_without_state_queries(self):
        body = ('发送操作返回错误，后续删除没有执行。\n'
                '我未重新核查目录或邮件状态，无法确认最终情况。\n')
        channel = ProbeChannel(channel_config(), episode_id='ep/blocked',
                               account_email='target@example.org')
        report = channel.report({}, {}, [receipt('本轮操作回执', body)])
        self.assertEqual(report['queries'], [])
        self.assertFalse(report['diagnostics']['main_attack_email_received'])
        packets = public_packets(report)
        self.assertEqual(len(packets), 1)
        current = replace(attempt('ep/blocked', None), packets=tuple(packets))
        prompts = []

        def decide(prompt, stage):
            prompts.append(prompt)
            return json.dumps(dict(goal_status='unknown', action='Revise',
                                   action_reason='Final state unverified'))

        assessment = assess(current, [history_item(current)], 3, decide,
                            feedback_view='receiver_report_only', structured=False)
        data = json.loads(prompts[0].split('PUBLIC INPUT:\n', 1)[1])
        response = data['current']['packets'][0]['response']
        self.assertEqual(json.loads(response['evidence'])['body'], body)
        self.assertIsNone(response['supported'])
        self.assertEqual(assessment.goal_status, 'unknown')
        self.assertNotIn('pre_state', data['current'])
        self.assertNotIn('end_state', data['current'])

    def test_natural_language_passes_without_any_state_parser(self):
        with patch('src.evaluation.legacy_probe_reports.parse_report',
                   side_effect=AssertionError('Online code must not parse states')):
            value = json.loads(packet('ep/1')['response']['evidence'])
        self.assertEqual(value['body'], RAW)
        self.assertEqual(value['subject'], '回传情况')
        self.assertEqual(value['sender'], 'target@example.org')
        self.assertEqual(value['recipient'], ADDRESS)
        self.assertEqual(value['episode_id'], 'ep/1')
        self.assertEqual(value['report_format'], REPORT_FORMAT)
        self.assertNotIn('reports', value)
        self.assertFalse(hasattr(probe_sampling, '_object_mentions'))

    def test_unauthorized_receipt_and_attachment_body_are_not_exposed(self):
        channel = ProbeChannel(channel_config(), episode_id='ep/1', account_email='target@example.org')
        result = channel.report({}, {}, [
            receipt('outside', 'UNAUTHORIZED_BODY', recipient='elsewhere@example.org'),
            receipt('main', 'ATTACHMENT_EMAIL_BODY', attachment_count=5),
            receipt('status', RAW), receipt('extra', 'OVER_REPORT_LIMIT')])
        projected = json.dumps(public_packets(result), ensure_ascii=False)
        self.assertEqual(len(public_packets(result)), 1)
        for hidden in ('UNAUTHORIZED_BODY', 'ATTACHMENT_EMAIL_BODY', 'OVER_REPORT_LIMIT'):
            self.assertNotIn(hidden, projected)

    def test_byte_limit_does_not_truncate_accepted_text(self):
        config = dict(channel_config(), max_body_bytes=256)
        channel = ProbeChannel(config, episode_id='ep/1', account_email='target@example.org')
        # 86 Chinese characters = 258 UTF-8 bytes; reject rather than alter the report.
        report = channel.report({}, {}, [receipt('long', '中' * 86), receipt('ok', '中' * 85)])
        self.assertEqual(report['deliveries'][0]['reason'], 'body_limit_exceeded')
        values = public_packets(report)
        self.assertEqual(len(values), 1)
        self.assertEqual(json.loads(values[0]['response']['evidence'])['body'], '中' * 85)

    def test_episode_channel_recipient_nonce_and_duplicate_binding(self):
        valid = packet('ep/1')
        validate_public_probe_packets([valid], channel_config(), episode_id='ep/1')
        for key, value in (('episode_id', 'ep/0'), ('channel_id', 'other'),
                           ('recipient', 'other@example.org'), ('nonce', 'b' * 16)):
            changed = deepcopy(valid)
            data = json.loads(changed['response']['evidence'])
            data[key] = value
            changed['response']['evidence'] = json.dumps(data)
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_public_probe_packets([changed], channel_config(), episode_id='ep/1')
        with self.assertRaises(ValueError):
            validate_public_probe_packets([valid, valid], dict(channel_config(), max_reports=2))
        with self.assertRaises(ValueError):
            replace(attempt('ep/1'), episode_id='ep/2')


class EpisodeIsolationTests(unittest.TestCase):
    def test_assessment_sees_only_current_episode_even_when_no_report_arrives(self):
        old = replace(attempt('ep/1', 'OLD_REPORT_SENTINEL: everything succeeded.'),
                      final_output='OLD_FINAL_SENTINEL')
        for body in (None, RAW):
            with self.subTest(body=body):
                current = replace(attempt('ep/2', body), analysis='OLD_ANALYSIS_SENTINEL',
                                  revision_plan={'summary': 'OLD_PLAN_SENTINEL'})
                captured = []
                def decide(prompt, stage):
                    captured.append(prompt)
                    return json.dumps(dict(goal_status='unknown', action='Retry', action_reason='uncertain'))
                assess(current, [history_item(old), history_item(current)], 3, decide,
                       feedback_view='receiver_report_only', structured=False)
                prompt = captured[0]
                for stale in ('OLD_REPORT_SENTINEL', 'OLD_FINAL_SENTINEL',
                              'OLD_ANALYSIS_SENTINEL', 'OLD_PLAN_SENTINEL'):
                    self.assertNotIn(stale, prompt)
                data = json.loads(prompt.split('PUBLIC INPUT:\n', 1)[1])
                self.assertEqual(data['assessment_scope'], 'current_episode_only')
                self.assertEqual(data['current']['episode_id'], 'ep/2')
                self.assertEqual(len(data['history']), 1)
                self.assertEqual(data['current']['packets'], list(current.packets))
                self.assertIn('freshly initialized environment', ' '.join(prompt.split()))

    def test_planner_history_keeps_old_report_without_filling_missing_current(self):
        old, current = attempt('ep/1', 'OLD_REPORT_SENTINEL'), attempt('ep/2', None)
        visible = revision_history(current, [history_item(old), history_item(current)],
                                   revise_decision(), 'receiver_report_only')
        memory = build_revision_memory(dict(current.case, case_id='current_case'), visible,
                                       feedback_protocol=FEEDBACK_PROTOCOL)
        summary = summarize_probe_feedback(memory, channel_config())
        self.assertEqual(summary['current_episode'], dict(episode_id='ep/2', returned=False, reports=[]))
        self.assertEqual(summary['historical_episodes'][0]['episode_id'], 'ep/1')
        self.assertEqual(summary['historical_episodes'][0]['reports'][0]['body'], 'OLD_REPORT_SENTINEL')
        self.assertNotIn('objects', summary)
        self.assertEqual(summary['next_execution'], 'fresh_environment')
        memory['attempts'][0]['revision_observation']['episode_id'] = 'wrong'
        with self.assertRaises(ValueError):
            summarize_probe_feedback(memory, channel_config())

    def test_same_payload_and_nonce_still_have_distinct_episode_reports(self):
        first, second = attempt('ep/1', 'earlier report'), attempt('ep/2', 'new report')
        self.assertEqual(first.payload, second.payload)
        visible = revision_history(second, [history_item(first), history_item(second)],
                                   revise_decision(), 'receiver_report_only')
        memory = build_revision_memory(dict(second.case, case_id='current_case'), visible,
                                       feedback_protocol=FEEDBACK_PROTOCOL)
        summary = summarize_probe_feedback(memory, channel_config())
        self.assertEqual(summary['current_episode']['reports'][0]['body'], 'new report')
        self.assertEqual(summary['historical_episodes'][0]['reports'][0]['body'], 'earlier report')


class RawScoringTests(unittest.TestCase):
    def test_raw_report_accuracy_is_unscored_and_legacy_parser_is_not_called(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            branch = root / 'branches/b000'
            folder = make_execution(branch, 1, reports=[], queries=[], writes=[], accepted=False)
            channel = ProbeChannel(channel_config(), episode_id='episode/1', account_email='target@example.org')
            report = channel.report({}, {}, [receipt('Report', RAW)])
            private = json.loads((folder / 'execution_private.json').read_text())
            private['raw']['probe_sampling'] = report
            write_json(folder / 'execution_private.json', private)
            public = json.loads((folder / 'feedback_public.json').read_text())
            public['packets'] = public_packets(report)
            write_json(folder / 'feedback_public.json', public)
            with patch('src.evaluation.probe_sampling.parse_report', side_effect=AssertionError):
                row = _score_execution('b000', folder, dict(mode='probe'), 'probe')
            self.assertTrue(row['delivery_accepted'])
            self.assertFalse(row['report_accuracy_scored'])
            self.assertIsNone(row['all_reported_accurate'])
            self.assertIsNone(row['joint_success'])
            self.assertFalse(row['public_projection_mismatch'])
            write_json(branch / 'job_public.json', dict(mode='probe', target_config=dict(probe_sampling=CONFIG)))
            write_json(branch / 'branch.json', dict(status='budget_exhausted', target_attempts=1,
                                                   decisions=[], generations=[]))
            write_json(root / 'manifest.json', dict(protocol=probe_sampling.PROTOCOL, mode='probe',
                                                   view='receiver_report_only', resumable=False, channel=CONFIG))
            write_json(root / 'status.json', dict(status='predictions_frozen'))
            write_json(branch / 'frozen.json', dict(files={
                str(path.relative_to(branch)): digest(path)
                for path in branch.rglob('*.json')}))
            result = score_run(root, root / 'metrics.json')
            self.assertEqual(result['legal_return_count'], 1)
            self.assertEqual(result['raw_report_accuracy_unscored_count'], 1)
            self.assertIsNone(result['report_accuracy_rate'])
            self.assertIsNone(result['joint_success_rate'])
            self.assertIsNone(result['mean_object_coverage'])
            self.assertTrue(all(value is None for value in result['object_coverage'].values()))


if __name__ == '__main__':
    unittest.main()
