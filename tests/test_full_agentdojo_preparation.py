"""Reference-population, public-only input and observable-scope regression checks."""
from copy import deepcopy
import unittest

from scripts import prepare_full_agentdojo as prep
from src.evidence.ledger import EvidenceLedger
from src.evidence.observer import FixedObserver, policy_for_case
from src.vendor.attacker_observer import Collector


class FullAgentDojoPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pool, cls.reference = prep.read_piminer_reference(prep.DEFAULT_REFERENCE)
        cls.references = prep.read_rows(prep.ROOT / 'data/dev.jsonl')
        cls.specifications = prep.read_rows(prep.ROOT / 'data/goal_specs.jsonl')
        cls.native_goals = prep.native_goal_lookup()

    def build(self, pool=None, exposed=()):
        return prep.build_corpus(self.pool if pool is None else pool, self.references,
            self.specifications, self.native_goals, set(exposed), {})

    def test_exact_original_30_public_snapshots_not_full1234_or_cached_attack(self):
        self.assertEqual([r['case_id'] for r in self.pool], [f'c{i:03}' for i in range(30)])
        self.assertEqual(self.reference['train_test_row_identity_overlap'], 0)
        for row in self.pool:
            self.assertFalse({'history', 'p0', 'strategy_ids', 'strategy_paths', 'status',
                              'gold', 'security', 'utility'} & set(row))

    def test_all_eligible_reference_rows_retained_without_replacements(self):
        cases, specs, excluded, families = self.build()
        self.assertEqual(len(cases), 22)
        self.assertEqual(len(specs), 22)
        self.assertEqual([r['case_id'] for r in excluded],
            ['c002', 'c003', 'c008', 'c009', 'c015', 'c023', 'c024', 'c027'])
        self.assertEqual({r['case_id'] for r in cases} | {r['case_id'] for r in excluded},
                         {r['case_id'] for r in self.pool})
        self.assertEqual(len([r for r in cases if r['suite_name'] in ('slack', 'travel')]), 3)
        self.assertTrue(all(r['split'] == 'benchmark' for r in cases))
        self.assertTrue(all(r['provenance']['independent_heldout'] is False for r in cases))
        self.assertEqual(sum(f['population_cases'] for f in families.values()), 30)

    def test_cached_outcomes_and_exposure_do_not_change_selected_rows(self):
        dirty = deepcopy(self.pool)
        for row in dirty:
            row.update(p0='cached payload', history=[{'security': True}], gold=True,
                       status='success', strategy_paths=['cached.md'])
        plain = self.build()
        modified = self.build(dirty, [r['base_task_id'] for r in dirty])
        self.assertEqual([r['case_id'] for r in plain[0]], [r['case_id'] for r in modified[0]])
        self.assertEqual(plain[1:], modified[1:])
        self.assertTrue(all(r['provenance']['known_prior_exposure'] for r in modified[0]))
        self.assertFalse(any('p0' in r or 'history' in r or 'gold' in r for r in modified[0]))

    def test_ungranted_required_mailbox_is_not_an_unknown_trigger_task(self):
        case = deepcopy(self.pool[0])
        case['resource_grants'] = {'mailboxes': []}
        cases, specs, excluded, _ = self.build([case])
        self.assertEqual(cases, [])
        self.assertEqual(specs, [])
        self.assertEqual(excluded[0]['reason'], 'no_supported_predicate')

    def test_existing_exact_family_definitions_retained(self):
        cases, specs, _, _ = self.build()
        by_id = {s['case_id']: s for s in specs}
        for original in self.specifications:
            if original['case_id'] not in by_id:
                continue
            new = by_id[original['case_id']]
            self.assertEqual(new['trigger_ids'], original['trigger_ids'])
            self.assertEqual(new['predicates'], original['predicates'])

    def test_changed_goal_cannot_reuse_family_predicate(self):
        case = deepcopy(self.pool[0])
        case['injection_task_goal'] += ' Extra hidden goal.'
        with self.assertRaisesRegex(ValueError, 'Public goal differs'):
            self.build([case])

    def test_travel_and_slack_absence_remains_unknown_not_false(self):
        cases, specs, _, _ = self.build()
        by_id = {s['case_id']: s for s in specs}
        for case in cases:
            if case['case_id'] not in ('c011', 'c025', 'c028'):
                continue
            collector = Collector(policy_for_case(case))
            ledger = EvidenceLedger('offline', by_id[case['case_id']])
            for packet in FixedObserver().collect('offline', case, collector.finish('Final')):
                ledger.update(packet)
            self.assertEqual(ledger.trigger, 'U')

    def test_private_or_proxy_goals_never_become_eligible_from_existing_resource(self):
        cases, _, excluded, _ = self.build([self.pool[i] for i in (2, 8, 23, 27)])
        self.assertEqual(cases, [])
        self.assertTrue(all(r['reason'] == 'no_observable_necessary_trigger' for r in excluded))


if __name__ == '__main__':
    unittest.main()
