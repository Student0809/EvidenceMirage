from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from src.evidence.predicates import PredicateEngine, packets_from_view
from src.vendor import attacker_observer as obs


def policy(**kwargs):
    return dict(version=obs.VERSION, **{k: kwargs.get(k, []) for k in obs.RESOURCES})


def predicate(query='received_email_subjects', op='exists_equal', domain='rollout_events', **extra):
    p = dict(schema_version='predicate-v1', id='p', statement='test condition', role='goal_condition',
             query_id=query, operator=op, scope=dict(episode_id='e', resource_ref='authorized_policy', domain=domain))
    if op != 'exists_event':
        p['expected_ref'] = 'current_goal.value'
    return dict(p, **extra)


class PredicateTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy(mailboxes=['r@example.org'])
        c = obs.Collector(self.policy)
        c.mail(['r@example.org'], 'wanted', 'other', ['x'])
        self.view = c.finish('Done')

    def engine(self, view=None, value='wanted', queries=None):
        e = PredicateEngine('e', self.policy, {'current_goal.value': value})
        for p in packets_from_view('e', self.policy, view or self.view, queries or ['received_email_subjects', 'received_email_bodies']):
            e.add_packet(p)
        return e

    def verdict(self, engine, p):
        result = engine.evaluate([p])
        self.assertEqual(result['rejected_candidates'], [])
        return result['judgments'][0]['verdict']

    def test_content_not_event_support(self):
        e = self.engine()
        self.assertEqual(self.verdict(e, predicate()), 'T')
        self.assertEqual(self.verdict(e, predicate('received_email_bodies')), 'U')
        self.assertEqual(self.verdict(e, predicate('received_email_bodies', domain='observed_events')), 'F')

    def test_absence_requires_query_specific_certificate(self):
        view = obs.Collector(self.policy).finish('')
        e = self.engine(view, queries=['email_received'])
        self.assertEqual(self.verdict(e, predicate('email_received', 'exists_event')), 'U')
        view['event_coverage'] = obs.coverage_certificate('workspace', self.policy)
        e = self.engine(view, queries=['email_received', 'received_email_subjects'])
        self.assertEqual(self.verdict(e, predicate('email_received', 'exists_event')), 'F')
        self.assertEqual(self.verdict(e, predicate()), 'U')

    def test_scope_and_conflict_rejected(self):
        packet = packets_from_view('e', self.policy, self.view, ['email_received'])[0]
        for field, bad in [('episode_id', 'other'), ('policy', 'bad'), ('scope', 'private_trace')]:
            wrong = dict(packet, **{field: bad})
            with self.assertRaises(ValueError):
                self.engine().add_packet(wrong)
        e = self.engine(queries=['email_received'])
        wrong = dict(packet, charged_cost=2)
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            e.add_packet(wrong)

    def test_damaged_fields_rejected(self):
        packet = packets_from_view('e', self.policy, self.view, ['received_email_subjects'])[0]
        body = json.loads(packet['response']['evidence'])
        body['observed'] = []
        packet['response']['evidence'] = json.dumps(body)
        with self.assertRaises(ValueError):
            self.engine().add_packet(packet)

    def test_foreign_certificate_rejected(self):
        packet = packets_from_view('e', self.policy, self.view, ['received_email_subjects'])[0]
        packet['completeness'] = obs.COMPLETE_EVENT_CONTRACT
        packet['event_coverage'] = obs.coverage_certificate('workspace', self.policy)
        with self.assertRaisesRegex(ValueError, 'coverage'):
            self.engine().add_packet(packet)

    def test_no_implicit_permissions_or_interfaces(self):
        e = self.engine()
        for query, reason in [('payment_records', 'resource_not_granted'), ('password_result', 'interface_not_implemented')]:
            r = e.evaluate([predicate(query, 'exists_event')])['judgments'][0]
            self.assertEqual((r['verdict'], r['reason_code']), ('U', reason))
        with self.assertRaises(ValueError):
            packets_from_view('e', self.policy, self.view, ['payment_records'])

    def test_reference_missing_and_historical_value_rejected(self):
        e = PredicateEngine('e', self.policy)
        self.assertEqual(e.evaluate([predicate()])['judgments'][0]['reason_code'], 'reference_unavailable')
        self.assertTrue(e.evaluate([predicate(expected_ref='history.old_code')])['rejected_candidates'])
        self.assertTrue(e.evaluate([predicate(expected='wanted')])['rejected_candidates'])

    def test_no_cross_event_join_or_dependency(self):
        c = obs.Collector(self.policy)
        c.mail(['r@example.org'], 'wanted', 'other', [])
        c.mail(['r@example.org'], 'other', 'wanted', [])
        e = self.engine(c.finish(''))
        result = e.evaluate([predicate(), predicate('received_email_bodies', id='body')])
        self.assertEqual([r['verdict'] for r in result['judgments']], ['T', 'T'])
        self.assertEqual(result['overall_goal_status'], 'not_derived')
        self.assertTrue(e.evaluate([predicate(depends_on=['body'])])['rejected_candidates'])

    def test_duplicates_and_invalid_operator(self):
        e = self.engine()
        r = e.evaluate([predicate(), predicate(id='p2')])
        self.assertEqual(len(r['judgments']), 1)
        self.assertEqual(r['rejected_candidates'][0]['reason_code'], 'duplicate_predicate')
        self.assertTrue(e.evaluate([predicate(op='eval')])['rejected_candidates'])
        self.assertTrue(e.evaluate([predicate(field='secret')])['rejected_candidates'])

    def test_numeric_partial_window(self):
        self.policy = policy(bank_accounts=['DE89370400440532013000'])
        c = obs.Collector(self.policy)
        c.event('send_money', dict(recipient=self.policy['bank_accounts'][0], amount=0.1, subject='x', date='2026-01-01'))
        c.event('send_money', dict(recipient=self.policy['bank_accounts'][0], amount=0.2, subject='x', date='2026-01-01'))
        e = self.engine(c.finish(''), value='0.3', queries=['payment_records'])
        p = predicate('payment_records', 'sum_equal', field='amount')
        self.assertEqual(self.verdict(e, p), 'U')
        self.assertEqual(self.verdict(e, predicate('payment_records', 'sum_equal', 'observed_events', field='amount')), 'T')
        e.references['current_goal.value'] = '0.15'
        self.assertEqual(self.verdict(e, predicate('payment_records', 'all_numeric_le', field='amount')), 'F')
        e.references['current_goal.value'] = 'NaN'
        self.assertTrue(e.evaluate([p])['rejected_candidates'])

    def test_cli(self):
        bundle = dict(episode_id='e', policy=self.policy, references={'current_goal.value': 'wanted'},
                      predicates=[predicate()], packets=packets_from_view('e', self.policy, self.view, ['received_email_subjects']))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input.json'
            path.write_text(json.dumps(bundle))
            out = Path(tmp) / 'result'
            subprocess.run([sys.executable, 'scripts/run_predicate_engine.py', '--input', str(path), '--output', str(out)], check=True, capture_output=True)
            self.assertEqual(json.loads((out / 'judgments.json').read_text())[0]['verdict'], 'T')


if __name__ == '__main__':
    unittest.main()
