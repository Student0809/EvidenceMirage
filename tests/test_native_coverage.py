"""Complete receiver-event absence needs audited, successful online collection."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agentdojo.default_suites
from agentdojo.task_suite import get_suite

from src.adapters.agentdojo import AgentDojoAdapter
from src.engine.config import ROOT, load_cases, load_config
from src.evidence.ledger import EvidenceLedger
from src.evidence.observer import FixedObserver, policy_for_case
from src.schema import Payload
from src.vendor import attacker_observer as obs


class NativeCoverageTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases(load_config(ROOT / 'experiments/e0_smoke.yaml'))

    def execute(self, index=0, action=None):
        case = self.cases[index]
        def query(prompt, runtime, env):
            if action:
                action(runtime, env)
            return prompt, runtime, env, [dict(role='assistant',
                content=[dict(type='text', content='I cannot comply.')], tool_calls=[])], {}
        result = AgentDojoAdapter(pipeline=SimpleNamespace(query=query)).execute(case, Payload('P0', 'x'), 'e')
        ledger = EvidenceLedger('e', case['goal_spec'])
        packets = FixedObserver().collect('e', case, result.observation)
        for packet in packets:
            ledger.update(packet)
        return result, ledger, packets

    def test_normal_native_no_event_is_false_for_local_mail_and_payment(self):
        for index, query in ((0, 'email_received'), (4, 'payment_received')):
            result, ledger, packets = self.execute(index)
            self.assertIsNone(result.error)
            self.assertFalse(result.gold)
            self.assertEqual(ledger.trigger, 'F')
            packet = next(p for p in packets if p['query_id'] == query)
            self.assertEqual(packet['completeness'], obs.COMPLETE_EVENT_CONTRACT)
            self.assertIs(packet['response']['supported'], False)
            self.assertEqual(packet['event_coverage']['scope'], 'this_rollout_authorized_receiver_events')

    def test_missing_receipt_makes_attachment_aggregate_false_without_inventing_attachment_fact(self):
        _, ledger, _ = self.execute(1)
        self.assertEqual(ledger.trigger, 'F')
        statuses = {p['id']: p['status'] for p in ledger.records()}
        self.assertEqual(statuses, {'receipt': 'F', 'received_attachment': 'U'})

    def test_native_received_mail_without_attachment_is_scoped_false(self):
        def action(runtime, env):
            _, error = runtime.run_function(env, 'send_email', dict(
                recipients=['mark.black-2134@gmail.com'], subject='Visible', body='Body'))
            self.assertIsNone(error)
        _, ledger, _ = self.execute(1, action)
        self.assertEqual({p['id']: p['status'] for p in ledger.records()},
                         {'receipt': 'T', 'received_attachment': 'F'})
        self.assertEqual(ledger.trigger, 'F')

    def test_native_receipt_survives_sent_mail_deletion(self):
        def action(runtime, env):
            sent, error = runtime.run_function(env, 'send_email', dict(
                recipients=['mark.black-2134@gmail.com'], subject='Visible', body='Body'))
            self.assertIsNone(error)
            _, error = runtime.run_function(env, 'delete_email', dict(email_id=sent.id_))
            self.assertIsNone(error)
        result, ledger, _ = self.execute(0, action)
        self.assertEqual(len(result.observation['receipts']), 1)
        self.assertEqual(ledger.trigger, 'T')

    def test_related_tool_error_degrades_absence_to_unknown(self):
        def action(runtime, env):
            _, error = runtime.run_function(env, 'send_email', dict(
                recipients=['mark.black-2134@gmail.com'], subject='Visible', body='Body',
                attachments=[{'type': 'file'}]))
            self.assertIsNotNone(error)
        result, ledger, packets = self.execute(0, action)
        self.assertIsNone(result.error)
        self.assertNotIn('event_coverage', result.observation)
        self.assertEqual(ledger.trigger, 'U')
        self.assertTrue(all(p['completeness'] == 'returned_event_fields_only' for p in packets))

    def test_capture_failure_keeps_gold_but_has_no_false_absence(self):
        def action(runtime, env):
            _, error = runtime.run_function(env, 'send_email', dict(
                recipients=['mark.black-2134@gmail.com'], subject='Visible', body='Body'))
            self.assertIsNone(error)
        with patch.object(obs.Collector, 'mail', side_effect=RuntimeError('capture failure')):
            result, ledger, packets = self.execute(0, action)
        self.assertIsNone(result.error)
        self.assertIsNotNone(result.observation_error)
        self.assertIsNone(result.observation)
        self.assertEqual(packets, ())
        self.assertEqual(ledger.trigger, 'U')

    def test_manual_and_offline_collectors_cannot_claim_complete_even_after_wrapping(self):
        case = self.cases[0]
        policy = policy_for_case(case)
        manual = obs.Collector(policy)
        manual.wrap_tools(get_suite('v1.2.2', 'workspace').tools)
        view = manual.finish('Done', rollout_complete=True)
        offline = obs.observe(policy, final_response='Done', messages=[])
        for candidate in (view, offline):
            self.assertNotIn('event_coverage', candidate)
            packet = next(p for p in FixedObserver().collect('e', case, candidate)
                          if p['query_id'] == 'email_received')
            self.assertIsNone(packet['response']['supported'])
            ledger = EvidenceLedger('e', case['goal_spec'])
            ledger.update(packet)
            self.assertEqual(ledger.trigger, 'U')

    def test_version_toolset_and_function_identity_changes_fail_closed(self):
        policy = policy_for_case(self.cases[0])
        tools = get_suite('v1.2.2', 'workspace').tools
        replaced = list(tools)
        replaced[0] = tools[0].model_copy(update={'run': lambda *args, **kwargs: None})
        duplicate = [tools[0], *tools[1:-1], tools[0]]
        for version, selected in (('v1.2.1', tools), ('v1.2.2', tools[:-1]),
                                  ('v1.2.2', replaced), ('v1.2.2', duplicate)):
            collector = obs.Collector.for_native_rollout(policy, benchmark_version=version, suite_name='workspace')
            collector.wrap_tools(selected)
            self.assertNotIn('event_coverage', collector.finish('Done', rollout_complete=True))
        with patch.object(obs.importlib.metadata, 'version', return_value='unknown'):
            result, ledger, _ = self.execute()
        self.assertNotIn('event_coverage', result.observation)
        self.assertEqual(ledger.trigger, 'U')

    def test_native_tool_identity_check_does_not_read_or_hash_source_bytes(self):
        tools = get_suite('v1.2.2', 'workspace').tools
        with patch('pathlib.Path.read_bytes', side_effect=AssertionError('No source byte authentication')):
            self.assertTrue(obs._audit_native_tools('v1.2.2', 'workspace', tools))
        _, ledger, packets = self.execute()
        self.assertEqual(ledger.trigger, 'F')
        self.assertTrue(all('tool_manifest_sha256' not in p['event_coverage'] for p in packets))

    def test_complete_requires_normal_finalization_and_single_full_wrap(self):
        policy = policy_for_case(self.cases[0])
        collector = obs.Collector.for_native_rollout(policy, benchmark_version='v1.2.2', suite_name='workspace')
        tools = get_suite('v1.2.2', 'workspace').tools
        collector.wrap_tools(tools)
        self.assertNotIn('event_coverage', collector.finish('Not yet finished'))
        collector.wrap_tools(tools)
        self.assertNotIn('event_coverage', collector.finish('Done', rollout_complete=True))

    def test_legacy_source_hash_is_ignored_without_relaxing_certificate_scope(self):
        _, _, packets = self.execute()
        packet = deepcopy(next(p for p in packets if p['query_id'] == 'email_received'))
        packet['event_coverage']['tool_manifest_sha256'] = 'obsolete source fingerprint'
        ledger = EvidenceLedger('e', self.cases[0]['goal_spec'])
        ledger.update(packet)
        self.assertEqual(ledger.trigger, 'F')
        for field, value in (('benchmark_version', 'unknown'), ('agentdojo_version', 'unknown'),
                             ('scope', 'all_private_actions'), ('rollout_complete', False),
                             ('query_ids', ['email_received', 'website_visited'])):
            tampered = deepcopy(packet)
            tampered['event_coverage'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                EvidenceLedger('e', self.cases[0]['goal_spec']).update(tampered)
        packet['query_id'] = 'website_visited'
        with self.assertRaisesRegex(ValueError, 'outside complete event coverage'):
            EvidenceLedger('e', self.cases[0]['goal_spec']).update(packet)


if __name__ == '__main__':
    unittest.main()
