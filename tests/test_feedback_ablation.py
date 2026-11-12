"""A/P intervention isolation and public-controller integration; no network."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.attacker.original import OriginalPIMinerAdapter
from src.attacker.piminer import PIMinerAdapter
from src.engine.feedback_runner import FeedbackRunner
from src.feedback.ablation import ABLATION_PROTOCOL, AssessmentModules, FeedbackAblation, GROUPS, NO_HISTORY
from src.mismatch.data import ROOT, jsonl, load_attempt
from src.mismatch.fixture import decide as fixture_decide
from src.mismatch.execution import run_prepared
from src.mismatch.data import write_json
from src.schema import Payload
from tests.test_mismatch_autonomous import judgment


def memory_document():
    return dict(schema_version=1, entries=[dict(id='historical-001',
        lesson='HISTORICAL_SENTINEL: returned call success alone may not establish all goal conditions.',
        applicability='A goal has multiple required conditions.',
        source_refs=['training-history/example-001'])])


class AblationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.row = jsonl(ROOT/'data/mismatch_autonomous_20261104/model_inputs.jsonl')[0]
        cls.attempt = replace(load_attempt(cls.row), strategy_document='PIMINER_LIBRARY_SENTINEL')

    def adapter(self, method, group, model, memory=None):
        kwargs = dict(decide=model, ablation=group, judgment_memory=None)
        return (PIMinerAdapter(ROOT, 'fixture', **kwargs) if method == 'v1' else
                OriginalPIMinerAdapter(ROOT, 'fixture', 'fixture', **kwargs))

    def test_four_groups_and_obsolete_memory_rejected(self):
        for group in GROUPS:
            modules = AssessmentModules(group)
            self.assertEqual(modules.config.tool_evidence, group[1] == '1')
            self.assertEqual(modules.config.historical_experience, group[2] == '1')
            with self.assertRaisesRegex(ValueError, 'obsolete'):
                AssessmentModules(group, memory_document())
        self.assertEqual(AssessmentModules().config.group, 'G11')
        with self.assertRaises(ValueError):
            FeedbackAblation.resolve('G12')

    def test_p_off_generation_never_reads_strategy_files(self):
        from src.schema import Payload
        calls = []
        def model(prompt, stage):
            calls.append(prompt)
            return '<injection>candidate</injection>'
        case = dict(self.attempt.case, strategy_paths=['missing-library-file'])
        for group in ('G00', 'G10'):
            with patch.object(Path, 'read_text', side_effect=AssertionError('library read')):
                PIMinerAdapter(ROOT, 'fixture', decide=model, ablation=group).generate(
                    case, None, None, [], 'P0')
        self.assertTrue(all(NO_HISTORY in p for p in calls))

    def test_all_groups_both_adapters_revise_through_existing_backend(self):
        for method in ('piminer', 'v1'):
            prompts = {}
            for group in GROUPS:
                calls = []
                def model(prompt, stage):
                    calls.append((stage, prompt))
                    return fixture_decide(prompt, stage)
                adapter = self.adapter(method, group, model, memory_document())
                result = FeedbackRunner(adapter, feedback_view='authorized_evidence').run(self.attempt)
                self.assertEqual(result.status, 'revision_ready', (method, group, result))
                self.assertEqual(result.ablation['group'], group)
                self.assertEqual(sum(stage == 'assessment' for stage, _ in calls), 1)
                prompt = next(p for s, p in calls if s == 'assessment')
                data = json.loads(prompt.split('PUBLIC INPUT:\n', 1)[1])
                template = prompt.split('PUBLIC INPUT:\n', 1)[0]
                if 'template' in prompts:
                    self.assertEqual(template, prompts['template'])
                prompts['template'] = template
                self.assertNotIn('judgment_experience', data)
                self.assertEqual(data['assessment_mode'], 'ordinary')
                self.assertEqual(bool(data['current']['packets']), group[1] == '1')
                self.assertEqual(data['current']['strategy_document'], self.attempt.strategy_document if group[2] == '1' else NO_HISTORY)
                for stage, content in calls:
                    if stage != 'assessment':
                        if group[2] == '0':
                            self.assertNotIn(self.attempt.strategy_document, content)
                if method == 'v1':
                    self.assertNotIn('semantic_review', [stage for stage, _ in calls])
                    for stage, content in calls:
                        if stage in ('planner', 'generator'):
                            self.assertEqual(json.dumps(self.attempt.strategy_document)[1:-1] in content, group[2] == '1')
                for field in ('remaining_target_budget',):
                    if field in prompts:
                        self.assertEqual(data[field], prompts[field])
                    prompts[field] = data[field]

    def test_default_and_explicit_g11_have_identical_v1_prompts(self):
        traces = []
        for group in (None, 'G11'):
            calls = []
            def model(prompt, stage):
                calls.append((stage, prompt))
                return fixture_decide(prompt, stage)
            result = FeedbackRunner(PIMinerAdapter(ROOT, 'fixture', decide=model, ablation=group),
                                    feedback_view='authorized_evidence').run(load_attempt(self.row))
            self.assertEqual(result.status, 'revision_ready')
            traces.append(calls)
        self.assertEqual(traces[0], traces[1])

    def test_stop_and_budget_remain_functional_in_all_groups(self):
        for group in GROUPS:
            for completed in (True, False):
                calls = []
                def model(prompt, stage):
                    calls.append(prompt)
                    value = judgment('completed' if completed else 'unknown', 'Stop' if completed else 'Retry')
                    value = {k: value[k] for k in ('goal_status', 'action', 'action_reason')}
                    return json.dumps(value)
                adapter = self.adapter('v1', group, model, memory_document())
                def source(current, payload, ordinal):
                    return replace(current, origin='target_execution', final_output='New public feedback')
                result = FeedbackRunner(adapter, source=source, target_budget=2).run(self.attempt)
                self.assertEqual(result.status, 'self_reported_complete' if completed else 'budget_exhausted')
                self.assertEqual(result.target_attempts, 0 if completed else 2)
                self.assertEqual(len(calls), 1 if completed else 3)
                self.assertTrue(all((NO_HISTORY in p) == (group[2] == '0') for p in calls))

    def test_ordinary_invalid_status_action_rejected_before_dispatch(self):
        for value in [dict(goal_status='completed', action='Revise', action_reason='bad'),
                      dict(goal_status='unknown', action='Stop', action_reason='bad')]:
            result = FeedbackRunner(self.adapter('v1', 'G00', lambda *a: json.dumps(value)),
                                    source=lambda *a: self.fail('must not dispatch')).run(self.attempt)
            self.assertEqual(result.status, 'judgment_error')
            self.assertEqual(result.target_attempts, 0)

    def test_default_keeps_v1_library_without_extra_judgment_bank(self):
        modules = AssessmentModules()
        self.assertEqual(modules.metadata()['group'], 'G11')
        calls = []
        def model(prompt, stage):
            calls.append(prompt)
            return json.dumps({k: v for k, v in judgment('unknown', 'Retry').items()
                               if k in ('goal_status', 'action', 'action_reason')})
        FeedbackRunner(PIMinerAdapter(ROOT, 'fixture', decide=model)).run(self.attempt)
        data = json.loads(calls[0].split('PUBLIC INPUT:\n', 1)[1])
        self.assertNotIn('judgment_experience', data)
        self.assertEqual(data['assessment_mode'], 'ordinary')

    def test_static_entry_freezes_effective_configuration_for_all_groups(self):
        original = Path.read_text
        def public_only(path, *args, **kwargs):
            if path.name in ('scoring_private.jsonl', 'pairing_private.json'):
                raise AssertionError('Evaluator labels read by decision runner')
            return original(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, 'read_text', public_only):
            for group in GROUPS:
                output = Path(directory)/group
                result = run_prepared(ROOT/'data/mismatch_autonomous_20261104', output,
                    method='v1', ablation=group, judgment_memory=None, fixture=True, limit=1)
                self.assertEqual(result['errors'], 0)
                manifest = json.loads((output/'manifest.json').read_text())
                self.assertEqual(manifest['ablation']['group'], group)
                self.assertEqual(manifest['cross_instance_experience'], group[2] == '1')
                self.assertFalse((output/'judgment_memory.json').exists())
                prediction = jsonl(output/'predictions.jsonl')[0]
                self.assertEqual(prediction['ablation'], manifest['ablation'])
                calls = json.loads(next((output/'cases').glob('*.calls.json')).read_text())
                self.assertEqual(NO_HISTORY in calls[0]['prompt'], group[2] == '0')

    def test_ordinary_compatibility_fields_are_not_scored_as_model_diagnostics(self):
        from src.evaluation.mismatch import summarize
        base = dict(label=dict(actual_goal_completed=False, claim_supported=None, disclosure='silent'),
            prediction=dict(assessment=judgment('unknown', 'Retry'), status='retry_requested',
                            model_calls=1, candidate_sha256=[]))
        for group in GROUPS:
            base['prediction']['ablation'] = AssessmentModules(group).metadata()
            metrics = summarize([base])
            self.assertEqual(metrics['unknown_count'], 1)
            self.assertEqual(metrics['structured_diagnostic_instances'], 0)
            self.assertIsNone(metrics['claim_support_accuracy_all'])
            self.assertIsNone(metrics['disclosure_accuracy'])

    def test_cli_g11_needs_no_memory_and_obsolete_input_fails_before_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root/'memory.json', memory_document())
            command = [sys.executable, str(ROOT/'main.py'), 'mismatch', 'run',
                       '--prepared', str(ROOT/'data/mismatch_autonomous_20261104'),
                       '--output', str(root/'run'), '--method', 'v1', '--ablation', 'G11',
                       '--fixture', '--limit', '1']
            result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['ablation']['group'], 'G11')
            with patch('src.attacker.transport.RoleDecision.__call__',
                       side_effect=AssertionError('Model must not run')):
                with self.assertRaises(ValueError):
                    run_prepared(ROOT/'data/mismatch_autonomous_20261104', root/'invalid',
                                 method='v1', ablation='G11', judgment_memory=memory_document())
            self.assertFalse((root/'invalid').exists())

    def test_online_branch_runs_all_groups_with_five_mocked_executions(self):
        from src.mismatch.online import run_branch
        for group in GROUPS:
            prompts = []
            def decide(prompt, stage):
                prompts.append(prompt)
                value = judgment('unknown', 'Retry')
                value = {k: value[k] for k in ('goal_status', 'action', 'action_reason')}
                return json.dumps(value)
            decide.calls = []
            def factory(*args, **kwargs):
                return PIMinerAdapter(*args, decide=decide, **kwargs)
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)/group
                write_json(folder/'job_public.json', dict(input=self.row, model='fixture', effort='max',
                    timeout=1, view=AssessmentModules(group).config.feedback_view, target_config={}, ablation=group,
                    ablation_protocol=ABLATION_PROTOCOL,
                    judgment_memory=None))
                def execute(command, **kwargs):
                    request_path = Path(command[-1])
                    request = json.loads(request_path.read_text())
                    from src.schema import Payload
                    write_json(request_path.parent/'execution_private.json', dict(gold=True,
                               secret='TEST_PRIVATE_SENTINEL'))
                    write_json(request_path.parent/'feedback_public.json', dict(final_output='New feedback',
                        packets=[], error=None, payload_sha256=Payload(**request['payload']).sha256))
                with patch('src.mismatch.online.configure'), \
                     patch('src.attacker.piminer.PIMinerAdapter', side_effect=factory), \
                     patch('src.mismatch.online.subprocess.run', side_effect=execute) as target:
                    result = run_branch(folder)
                self.assertEqual(result['status'], 'budget_exhausted')
                self.assertEqual(result['target_attempts'], 5)
                self.assertEqual(result['decisions'], 6)
                self.assertEqual(result['ablation']['group'], group)
                self.assertEqual(target.call_count, 5)
                self.assertNotIn('TEST_PRIVATE_SENTINEL', str(prompts))
                self.assertTrue(all((NO_HISTORY in p) == (group[2] == '0') for p in prompts))

    def test_tool_evidence_isolated_across_two_rounds_and_all_model_stages(self):
        from copy import deepcopy
        from src.feedback.history import history_item
        from src.feedback.assessment import validate_ordinary
        for method in ('piminer', 'v1'):
            for group in GROUPS:
                calls = []
                def model(prompt, stage):
                    calls.append((stage, prompt))
                    return fixture_decide(prompt, stage)
                adapter = self.adapter(method, group, model)
                packet = deepcopy(self.attempt.packets[0])
                packet['response']['evidence'] = 'INITIAL_TOOL_EVIDENCE_SENTINEL'
                initial = replace(self.attempt, packets=(packet,),
                                  final_output='INITIAL_FINAL_SENTINEL', analysis='P0_ANALYSIS_SENTINEL')
                live_packet = deepcopy(packet)
                live_packet['response']['evidence'] = 'LIVE_TOOL_EVIDENCE_SENTINEL'
                def source(current, payload, ordinal):
                    return replace(current, payload=payload, origin='target_execution',
                                   packets=(live_packet,), final_output='LIVE_FINAL_SENTINEL')
                result = FeedbackRunner(adapter, feedback_view='authorized_evidence',
                                        target_budget=1, source=source).run(initial)
                self.assertEqual(result.status, 'budget_exhausted', (group, result))
                # Exercise a second revision over the accumulated public history as well.
                decision = validate_ordinary(dict(goal_status='unknown', action='Revise', action_reason='test'))
                first = result.generations[0]
                live = replace(source(initial, Payload('P1', first['payload']), 1),
                               revision_plan=first.get('revision_plan'))
                history = [history_item(initial, decision), history_item(live, decision)]
                adapter.revise(live, history, decision, feedback_view='authorized_evidence')
                text = str(calls)
                self.assertIn('INITIAL_FINAL_SENTINEL', text)
                self.assertIn('LIVE_FINAL_SENTINEL', text)
                self.assertIn('P0_ANALYSIS_SENTINEL', text)
                for marker in ('INITIAL_TOOL_EVIDENCE_SENTINEL', 'LIVE_TOOL_EVIDENCE_SENTINEL'):
                    self.assertEqual(marker in text, group[1] == '1', (method, group, marker))
                self.assertEqual('PIMINER_LIBRARY_SENTINEL' in text, group[2] == '1')
                if group[1] == '0':
                    for stage, prompt in calls:
                        self.assertNotIn('returned_call_fields_only', prompt, stage)
                    self.assertNotIn('event_0', text)
                # The intervention projects inputs; it never destroys retained raw evidence.
                self.assertEqual(initial.packets[0]['response']['evidence'], 'INITIAL_TOOL_EVIDENCE_SENTINEL')

    def test_g00_prompts_do_not_change_when_hidden_packets_change(self):
        from copy import deepcopy
        traces = []
        for marker in ('HIDDEN_VARIANT_A', 'HIDDEN_VARIANT_B'):
            packet = deepcopy(self.attempt.packets[0])
            packet['response']['evidence'] = marker
            calls = []
            def model(prompt, stage):
                calls.append((stage, prompt))
                return fixture_decide(prompt, stage)
            result = FeedbackRunner(self.adapter('v1', 'G00', model),
                feedback_view='authorized_evidence').run(replace(self.attempt, packets=(packet,)))
            self.assertEqual(result.status, 'revision_ready')
            traces.append(calls)
        self.assertEqual(traces[0], traces[1])

    def test_conflicting_view_rejected_before_output_or_model(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'bad'
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                run_prepared(ROOT/'data/mismatch_autonomous_20261104', output,
                             method='v1', ablation='G00', feedback_view='authorized_evidence',
                             decide=lambda *a: self.fail('model called'))
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
