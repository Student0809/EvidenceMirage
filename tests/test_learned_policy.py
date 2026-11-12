"""Runtime integration checks for learned decision artifacts and existing freezing."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from src.engine.config import ROOT, load_config, load_cases, freeze, check_frozen
from src.engine.history import History, ObservationSummary, EvidenceSummary
from src.policies.learned import LearnedDecision, validate_runtime
from src.decision_learning.model import fit_model
from src.decision_learning.smoke import synthetic_rows
from src.schema import Action


class LearnedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.config = load_config(ROOT / 'experiments/e0_smoke.yaml')
        self.model = fit_model(synthetic_rows(self.config), steps=300)
        self.model_path = self.path / 'model.json'
        self.model_path.write_text(json.dumps(self.model))

    def history(self, state='T', stage='TRIGGERED_FAIL', remaining=3):
        evidence = (EvidenceSummary('receipt', 'email_received', state, True),)
        return History((ObservationSummary('e', 'a'*64, stage, state, 'Reject', evidence),),remaining,0)

    def test_policy_uses_fitted_action_values_and_explicit_fallback(self):
        policy = LearnedDecision(str(self.model_path), allow_synthetic=True)
        self.assertEqual(policy.decide(self.history('T')).action, Action.RETRY)
        self.assertEqual(policy.decide(self.history('F')).action, Action.REVISE)
        unknown = policy.decide(self.history('U'))
        self.assertEqual(unknown.action, Action.RETRY)
        self.assertIn('unavailable_fallback', unknown.reason)
        self.assertIn('q_retry=', policy.decide(self.history()).reason)
        tied = LearnedDecision(str(self.model_path), allow_synthetic=True, tie_margin=1,
                               fallback_action='Revise')
        self.assertEqual(tied.decide(self.history()).action, Action.REVISE)

    def test_existing_stop_contract_and_invalid_model_options(self):
        policy = LearnedDecision(str(self.model_path), allow_synthetic=True)
        for history in (self.history(stage='SUCCESS'), self.history(stage='ERROR'),
                        self.history(remaining=0)):
            self.assertEqual(policy.decide(history).action, Action.STOP)
        with self.assertRaisesRegex(ValueError, 'fixture-only'):
            LearnedDecision(str(self.model_path))
        with self.assertRaises(ValueError):
            LearnedDecision(str(self.model_path), allow_synthetic=True, tie_margin=float('nan'))

    def test_no_trigger_or_disabled_queries_rejected_and_synthetic_never_live(self):
        cases = load_cases(self.config)
        self.config['policies'] = {'learned': {'model_path':str(self.model_path), 'allow_synthetic':True}}
        validate_runtime(self.config, [cases[0]])
        with self.assertRaisesRegex(ValueError, 'observable Trigger'):
            validate_runtime(self.config, [cases[2]])
        disabled = deepcopy(self.config)
        disabled['observation'] = {'queries': []}
        altered = deepcopy(self.model)
        altered['protocol']['observation'] = {'queries': []}
        self.model_path.write_text(json.dumps(altered))
        with self.assertRaisesRegex(ValueError, 'not available'):
            validate_runtime(disabled, [cases[0]])
        self.config['backend'] = 'agentdojo'
        with self.assertRaisesRegex(ValueError, 'cannot run live'):
            validate_runtime(self.config, [cases[0]])

    def test_runtime_protocol_mismatch_and_model_training_overlap(self):
        case = load_cases(self.config)[0]
        self.config['policies'] = {'learned': {'model_path':str(self.model_path), 'allow_synthetic':True}}
        wrong = deepcopy(self.config)
        wrong['target']['model'] = 'changed-target'
        with self.assertRaisesRegex(ValueError, 'protocol differs'):
            validate_runtime(wrong, [case])
        self.model['training_base_task_ids'].append(case['base_task_id'])
        self.model_path.write_text(json.dumps(self.model))
        self.config['split'] = 'test'
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            validate_runtime(self.config, [case])

    def test_frozen_manifest_covers_trained_model_bytes(self):
        case = load_cases(self.config)[0]
        spec = case.pop('goal_spec')
        dataset, specs = self.path / 'cases.jsonl', self.path / 'goals.jsonl'
        dataset.write_text(json.dumps(case) + '\n')
        specs.write_text(json.dumps(spec) + '\n')
        self.config.update(dataset=str(dataset.relative_to(ROOT)),
                           goal_specs=str(specs.relative_to(ROOT)),
                           policies={'learned':dict(model_path=str(self.model_path.relative_to(ROOT)),
                                                    allow_synthetic=True)})
        manifest = freeze(self.config)
        self.assertIn(str(self.model_path.relative_to(ROOT)), manifest['inputs_sha256'])
        self.model['heads']['Retry']['weights'][0] += 0.01
        self.model_path.write_text(json.dumps(self.model))
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            check_frozen(manifest)


if __name__ == '__main__':
    unittest.main()
