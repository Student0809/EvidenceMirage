"""The exposed public benchmark scope must not weaken held-out test checks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from src.engine.config import (
    BENCHMARK_SCOPE, ROOT, check_frozen, digest, freeze, load_config, load_jsonl, validate,
)
from src.engine.history import History, ObservationSummary
from src.evidence.ledger import EvidenceLedger
from src.policies.stage_aware import StageAware


class BenchmarkConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=ROOT / 'data')
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = load_config(ROOT / 'experiments/evidence_repeat_asr3_pilot5_20261030.yaml')
        self.config.update(split='benchmark', evaluation_scope=BENCHMARK_SCOPE,
                           post_failure_target_budget=4)
        self.cases = load_jsonl(ROOT / self.config['dataset'])[:1]
        self.goals = load_jsonl(ROOT / self.config['goal_specs'])[:1]
        self.cases[0]['split'] = 'benchmark'
        self.config['dataset'] = str((self.directory / 'cases.jsonl').relative_to(ROOT))
        self.config['goal_specs'] = str((self.directory / 'goals.jsonl').relative_to(ROOT))
        self.config['benchmark_audit'] = str((self.directory / 'coverage.json').relative_to(ROOT))
        self.write_inputs()

    def write_inputs(self):
        for field, rows in (('dataset', self.cases), ('goal_specs', self.goals)):
            (ROOT / self.config[field]).write_text(''.join(json.dumps(r) + '\n' for r in rows))
        self.audit = dict(status='complete', evaluation_scope=BENCHMARK_SCOPE,
            independent_heldout=False, dataset_sha256=digest(ROOT / self.config['dataset']),
            goal_specs_sha256=digest(ROOT / self.config['goal_specs']),
            selected_case_ids=[c['case_id'] for c in self.cases],
            benchmark_version=self.config['target']['benchmark_version'],
            selection_rule='All fixed source test rows with an observable necessary Trigger.',
            population_cases=len(self.cases) + 1, selected_cases=len(self.cases), excluded_cases=1)
        self.write_audit()

    def write_audit(self):
        (ROOT / self.config['benchmark_audit']).write_text(json.dumps(self.audit))

    def test_audited_benchmark_accepts_known_exposure_and_repeated_base_tasks(self):
        second = deepcopy(self.cases[0])
        second['case_id'] = 'c000_second'
        second_goal = deepcopy(self.goals[0])
        second_goal['case_id'] = second['case_id']
        self.cases.append(second)
        self.goals.append(second_goal)
        self.write_inputs()
        cases = validate(self.config)
        self.assertEqual(len(cases), 2)
        self.assertEqual(cases[0]['base_task_id'], cases[1]['base_task_id'])
        self.assertTrue(cases[0]['provenance']['previously_evaluated'])

    def test_missing_audit_and_stale_dataset_or_goal_hash_are_rejected(self):
        missing = deepcopy(self.config)
        del missing['benchmark_audit']
        with self.assertRaisesRegex(ValueError, 'population audit'):
            validate(missing)
        original = deepcopy(self.audit)
        for field in ('dataset_sha256', 'goal_specs_sha256', 'selected_case_ids',
                      'benchmark_version', 'evaluation_scope', 'status'):
            with self.subTest(field=field):
                self.audit = deepcopy(original)
                self.audit[field] = ['different'] if field == 'selected_case_ids' else 'different'
                self.write_audit()
                with self.assertRaisesRegex(ValueError, 'population audit'):
                    validate(self.config)

    def test_population_counts_and_independent_claim_must_be_honest(self):
        original = deepcopy(self.audit)
        for field, value in (('population_cases', 0), ('selected_cases', 2),
                             ('excluded_cases', -1), ('selected_cases', True),
                             ('independent_heldout', True), ('selection_rule', ' ')):
            with self.subTest(field=field):
                self.audit = deepcopy(original)
                self.audit[field] = value
                self.write_audit()
                with self.assertRaisesRegex(ValueError, 'population'):
                    validate(self.config)

    def test_benchmark_requires_native_backend_and_explicit_observable_scope(self):
        for field, value in (('backend', 'fixture'), ('require_observable_trigger', False),
                             ('evaluation_scope', 'independent_test')):
            with self.subTest(field=field):
                config = deepcopy(self.config)
                config[field] = value
                with self.assertRaisesRegex(ValueError, 'Benchmark split'):
                    validate(config)

    def test_benchmark_cannot_include_a_task_without_observable_trigger(self):
        self.config['observation'] = dict(queries=[])
        with self.assertRaisesRegex(ValueError, 'observable necessary Trigger'):
            validate(self.config)

    def enable_unobservable_population(self):
        self.config.update(require_observable_trigger=False, allow_unobservable_trigger=True,
                           observation=dict(queries=[]))
        ids = [c['case_id'] for c in self.cases]
        self.audit.update(allow_unobservable_trigger=True, unobservable_case_ids=ids,
                          population_case_ids=ids, population_cases=len(ids),
                          selected_cases=len(ids), excluded_cases=0)
        self.write_audit()

    def test_explicit_full_population_opt_in_keeps_unobservable_trigger_unknown(self):
        self.enable_unobservable_population()
        cases = validate(self.config)
        self.assertEqual(cases[0]['goal_spec']['trigger_ids'], [])
        ledger = EvidenceLedger('unknown-example', cases[0]['goal_spec'])
        self.assertEqual(ledger.trigger, 'U')
        history = []
        policy = StageAware()
        for spent, (payload_hash, expected) in enumerate((('p0', 'Retry'), ('p0', 'Revise'),
                                                          ('p1', 'Retry'))):
            history.append(ObservationSummary(str(spent), payload_hash, 'UNOBSERVABLE',
                                               ledger.trigger, 'OTHER'))
            decision = policy.decide(History(tuple(history), 4-spent, spent))
            self.assertEqual(decision.action.value, expected)
            self.assertEqual(decision.reason, 'unknown_fixed_schedule')

    def test_unobservable_population_needs_matching_config_and_audit_opt_in(self):
        self.enable_unobservable_population()
        original = deepcopy(self.audit)
        for field, value in (('allow_unobservable_trigger', False),
                             ('unobservable_case_ids', []),
                             ('unobservable_case_ids', ['stale-case']),
                             ('population_case_ids', ['stale-case']),
                             ('population_cases', 2), ('excluded_cases', 1)):
            with self.subTest(field=field, value=value):
                self.audit = deepcopy(original)
                self.audit[field] = value
                self.write_audit()
                with self.assertRaisesRegex(ValueError, 'population'):
                    validate(self.config)
        self.audit = original
        self.write_audit()
        del self.config['allow_unobservable_trigger']
        with self.assertRaisesRegex(ValueError, 'observable necessary Trigger'):
            validate(self.config)

    def test_unknown_case_audit_is_checked_after_capability_projection(self):
        self.enable_unobservable_population()
        self.config['observation'] = load_config(ROOT / 'experiments/evidence_repeat_asr3_pilot5_20261030.yaml')['observation']
        with self.assertRaisesRegex(ValueError, 'full-population audit'):
            validate(self.config)

    def test_unobservable_opt_in_cannot_relax_heldout_test(self):
        self.enable_unobservable_population()
        self.config['split'] = 'test'
        with self.assertRaisesRegex(ValueError, 'only to the audited benchmark'):
            validate(self.config)

    def test_unobservable_opt_in_requires_explicit_false_observability_requirement(self):
        self.enable_unobservable_population()
        self.config['require_observable_trigger'] = True
        with self.assertRaisesRegex(ValueError, 'observable necessary Trigger'):
            validate(self.config)

    def test_strict_test_still_rejects_prior_exposure(self):
        self.cases[0]['split'] = 'test'
        self.config['split'] = 'test'
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'prior evaluation exposure'):
            validate(self.config)

    def test_population_audit_bytes_are_part_of_frozen_inputs(self):
        manifest = freeze(self.config)
        self.assertIn(self.config['benchmark_audit'], manifest['inputs_sha256'])
        self.assertEqual(len(check_frozen(manifest)), 1)
        self.audit['extra_provenance_note'] = 'Changed after freeze.'
        self.write_audit()
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            check_frozen(manifest)


if __name__ == '__main__':
    unittest.main()
