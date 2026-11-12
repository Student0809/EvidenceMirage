from copy import deepcopy
import unittest
from unittest.mock import patch

from src.engine.config import ROOT, load_cases, load_config, load_jsonl


class ObservableMainConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / 'experiments/e0_smoke.yaml')
        self.cases = load_jsonl(ROOT / 'data/dev.jsonl')
        self.specs = load_jsonl(ROOT / 'data/goal_specs.jsonl')

    def load_one(self, index, *, required=True, queries=None, test=False):
        case = deepcopy(self.cases[index])
        config = deepcopy(self.config)
        config['require_observable_trigger'] = required
        if queries is not None:
            config['observation'] = {'queries': queries}
        if test:
            config['split'] = case['split'] = 'test'
        with patch('src.engine.config.load_jsonl', side_effect=[[case], deepcopy(self.specs)]):
            return load_cases(config)[0]

    def test_observable_receipt_remains_and_hidden_steps_are_absent(self):
        case = self.load_one(0)
        self.assertEqual(case['goal_spec']['trigger_ids'], ['receipt'])

    def test_proxy_only_task_is_rejected_before_execution(self):
        with self.assertRaisesRegex(ValueError, 'observable necessary Trigger: c002'):
            self.load_one(2)

    def test_test_split_cannot_disable_observability_requirement(self):
        with self.assertRaisesRegex(ValueError, 'observable necessary Trigger: c002'):
            self.load_one(2, required=False, test=True)

    def test_disabled_query_is_excluded_from_local_trigger(self):
        case = self.load_one(1, queries=['email_received'])
        self.assertEqual(case['goal_spec']['trigger_ids'], ['receipt'])
        self.assertIn('received_attachment', case['goal_spec']['excluded_unobservable_trigger_ids'])

    def test_all_queries_disabled_cannot_enter_main_evaluation(self):
        with self.assertRaisesRegex(ValueError, 'observable necessary Trigger: c000'):
            self.load_one(0, queries=[])

    def test_historical_dev_without_trigger_remains_inspectable(self):
        case = self.load_one(2, required=False)
        self.assertEqual(case['goal_spec']['trigger_ids'], [])


if __name__ == '__main__':
    unittest.main()
