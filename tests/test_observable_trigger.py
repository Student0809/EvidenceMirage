"""Static legal-channel eligibility is separate from runtime missing evidence."""
from copy import deepcopy
import unittest

from src.engine.config import ROOT, load_cases, load_config
from src.evidence.ledger import EvidenceLedger
from src.evidence.observer import FixedObserver, policy_for_case, project_observable_goal_spec
from src.evidence.stage import estimate_stage
from src.vendor.attacker_observer import Collector


class ObservableTriggerTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases(load_config(ROOT / 'experiments/e0_smoke.yaml'))
        self.case = deepcopy(self.cases[0])

    def ledger(self, case, *, receipts=False, queries=None):
        observer = FixedObserver() if queries is None else FixedObserver(queries)
        case = deepcopy(case)
        case['goal_spec'] = project_observable_goal_spec(case, observer.queries)
        collector = Collector(policy_for_case(case))
        if receipts:
            collector.mail(collector.policy['mailboxes'], 'Visible receipt', 'Body', [])
        ledger = EvidenceLedger('local', case['goal_spec'])
        for packet in observer.collect('local', case, collector.finish('I refuse.')):
            ledger.update(packet)
        return ledger

    def test_mail_receipt_is_local_trigger_despite_unobserved_read_and_delete(self):
        ledger = self.ledger(self.case, receipts=True)
        self.assertEqual(ledger.goal_spec['trigger_ids'], ['receipt'])
        self.assertEqual(ledger.trigger, 'T')
        self.assertEqual(estimate_stage(False, ledger.trigger), 'TRIGGERED_FAIL')
        self.assertEqual([p['id'] for p in ledger.records() if p['is_trigger']], ['receipt'])

    def test_ungranted_necessary_channel_is_excluded_not_aggregated_unknown(self):
        self.case['goal_spec']['predicates'].append(dict(
            id='ungranted_payment', query='payment_received', kind='event',
            depends_on=[], public_goal_quote=self.case['injection_task_goal']))
        self.case['goal_spec']['trigger_ids'].append('ungranted_payment')
        ledger = self.ledger(self.case, receipts=True)
        self.assertEqual(ledger.trigger, 'T')
        records = {p['id']: p for p in ledger.records()}
        self.assertFalse(records['ungranted_payment']['is_trigger'])
        self.assertEqual(records['ungranted_payment']['status'], 'U')
        self.assertEqual(ledger.goal_spec['excluded_unobservable_trigger_ids'], ['ungranted_payment'])

    def test_empty_runtime_receipt_does_not_remove_statically_observable_trigger(self):
        ledger = self.ledger(self.case)
        self.assertEqual(ledger.goal_spec['trigger_ids'], ['receipt'])
        self.assertEqual(ledger.trigger, 'U')
        self.assertTrue(ledger.records()[0]['is_trigger'])

    def test_disabled_query_excludes_trigger_before_any_observation(self):
        projected = project_observable_goal_spec(self.case, [])
        self.assertEqual(projected['trigger_ids'], [])
        self.assertEqual(projected['excluded_unobservable_trigger_ids'], ['receipt'])

    def test_proxy_website_visit_cannot_make_c002_eligible(self):
        case = deepcopy(next(c for c in self.cases if c['case_id'] == 'c002'))
        projected = project_observable_goal_spec(case)
        collector = Collector(policy_for_case(case))
        collector.event('get_webpage', {'url': 'www.secure-systems-252.com'})
        ledger = EvidenceLedger('proxy', projected)
        for packet in FixedObserver().collect('proxy', case, collector.finish('Visited.')):
            ledger.update(packet)
        self.assertEqual(projected['trigger_ids'], [])
        self.assertEqual(ledger.trigger, 'U')
        self.assertEqual(ledger.records()[0]['status'], 'T')
        self.assertFalse(ledger.records()[0]['is_trigger'])

    def test_dependency_remains_part_of_observability_contract(self):
        case = deepcopy(self.cases[1])
        projected = project_observable_goal_spec(case, ['received_attachment_counts'])
        self.assertEqual(projected['trigger_ids'], [])
        self.assertEqual(set(projected['excluded_unobservable_trigger_ids']),
                         {'receipt', 'received_attachment'})

    def test_projection_preserves_input_and_is_idempotent(self):
        before = deepcopy(self.case)
        projected = project_observable_goal_spec(self.case, [])
        self.assertEqual(self.case, before)
        self.case['goal_spec'] = projected
        self.assertEqual(project_observable_goal_spec(self.case, []), projected)


if __name__ == '__main__':
    unittest.main()
