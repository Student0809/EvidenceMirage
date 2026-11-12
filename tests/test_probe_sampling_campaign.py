"""Offline end-to-end branch test for the sampling campaign (no model calls)."""
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from src.evidence.probe_sampling import PROTOCOL, public_packets
from src.feedback.ablation import ABLATION_PROTOCOL
from src.mismatch.data import write_json
from src.mismatch.sampling import run_sampling_branch
from src.schema import Payload
from tests.test_mismatch_autonomous import V1Scripted, judgment

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / 'configs/mismatch/probe_sampling_g11.json').read_text())
ADDRESS = CONFIG['address']
NONCE = 'a' * 16
RECEIPT_BODY = ('One deletion returned success; another returned object not found. '
                'Remaining actions were not attempted. Final state was not rechecked.')


def public_case():
    value = json.loads((ROOT / 'reports/mismatch_p0_20261104/inputs/S01.json').read_text())
    return {key: value[key] for key in (
        'suite_name', 'user_task_id', 'injection_task_id', 'user_task_prompt',
        'injection_task_goal', 'context_with_placeholder', 'injection_candidates')}


class ScriptedDecide:
    def __init__(self):
        self.calls = []

    def __call__(self, prompt, stage):
        self.calls.append(dict(stage=stage, prompt=prompt))
        return 'scripted'


class FakeAdapter:
    def __init__(self, *args, **kwargs):
        self.decide = ScriptedDecide()
        self.assessment_modules = None

    def generate(self, case, payload, episode, packets, revision_id, *, strategy_document=None,
                 final_output=None, revision_history=None, feedback_protocol=None, sampling=None):
        self.decide('p0 prompt', 'generator_p0')
        text = (f'candidate {revision_id} marker {sampling.nonce}'
                if sampling is not None else f'candidate {revision_id}')
        return Payload(revision_id, text), dict(
            status='ready', payload=text, payload_sha256=Payload(revision_id, text).sha256,
            analysis='', sampling=sampling.public() if sampling is not None else None)

    def revise(self, attempt, history, decision, *, feedback_view, sampling=None):
        self.decide('revise prompt', 'attacker_revise')
        text = (f'revised {len(history)} marker {sampling.nonce}'
                if sampling is not None else f'revised {len(history)}')
        return Payload(f'P{len(history)}', text), dict(
            status='ready', payload=text, payload_sha256=Payload(f'P{len(history)}', text).sha256,
            sampling=sampling.public() if sampling is not None else None)

    def assess_and_decide(self, attempt, history, budget, *, feedback_view):
        action = 'Revise' if budget > 0 else 'Stop'
        return SimpleNamespace(action=action, to_dict=lambda: dict(action=action))


class RealDecide(V1Scripted):
    """PIMiner planner/generator script with a free-form probe marker."""
    @staticmethod
    def _marker(prompt):
        match = re.search(r'episode marker ([0-9a-f]{16,32})', prompt)
        return match.group(1) if match else ''

    def __call__(self, prompt, stage):
        if stage == 'generator_p0':
            self.prompts.append((stage, prompt))
            return f'<injection>payload marker {self._marker(prompt)}</injection>'
        if stage in ('generator', 'static_repair'):
            self.prompts.append((stage, prompt))
            return f'<injection>revised marker {self._marker(prompt)}</injection>'
        if stage == 'assessment':
            self.prompts.append((stage, prompt))
            value = judgment()
            return json.dumps({key: value[key]
                               for key in ('goal_status', 'action', 'action_reason')})
        return super().__call__(prompt, stage)


class CampaignBranchTests(unittest.TestCase):
    def branch(self, root, mode):
        job = dict(protocol=PROTOCOL, mode=mode,
                   input=dict(case=public_case(), strategy_document='STRATEGY'),
                   model='fixture', effort='max', timeout=1,
                   view='receiver_report_only' if mode == 'probe' else 'final_only',
                   ablation='G11' if mode == 'probe' else 'G10',
                   ablation_protocol=ABLATION_PROTOCOL,
                   target_config=dict(probe_sampling=CONFIG) if mode == 'probe' else {},
                   budget=3)
        write_json(root / 'job_public.json', job)
        return root

    def fake_target(self, command, **kwargs):
        request_path = Path(command[-1])
        folder = request_path.parent
        request = json.loads(request_path.read_text())
        payload = Payload(**request['payload'])
        if request.get('feedback_projection') == 'probe_receiver':
            nonce = request['target_config']['probe_sampling']['nonce']
            delivery = dict(delivery_id='delivery_0', sequence=2,
                            received_at='2026-01-01T00:00:00+00:00',
                            sender='target@example.org', recipient=ADDRESS,
                            subject='status',
                            body=RECEIPT_BODY, accepted=True)
            packets = public_packets(dict(status='completed', channel_id=CONFIG['channel_id'],
                                          nonce=nonce, episode_id=request['episode_id'], deliveries=[delivery]))
        else:
            packets = []
        write_json(folder / 'execution_private.json', dict(gold=False, error=None, raw={}))
        write_json(folder / 'feedback_public.json', dict(
            final_output='Target text.', packets=packets, error=None,
            payload_sha256=payload.sha256))

    def run_branch(self, mode):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        root = self.branch(Path(directory) / 'b000', mode)
        with patch('src.mismatch.sampling.PIMinerAdapter', FakeAdapter), \
             patch('src.mismatch.sampling.configure'), \
             patch('src.mismatch.sampling.subprocess.run', side_effect=self.fake_target):
            result = run_sampling_branch(root)
        return root, result

    def test_probe_branch_counts_p0_and_uses_receiver_packets(self):
        root, result = self.run_branch('probe')
        self.assertEqual(result['target_attempts'], 3)
        self.assertEqual(result['decisions'], 3)
        self.assertEqual(result['status'], 'self_reported_complete')
        self.assertTrue((root / 'p0_generation.json').exists())
        self.assertTrue((root / 'frozen.json').exists())
        executions = sorted((root / 'executions').glob('*'))
        self.assertEqual([path.name for path in executions], ['01', '02', '03'])
        attempts = json.loads((root / 'attempts_public.json').read_text())
        self.assertTrue(all(row['probe_present'] for row in attempts))
        self.assertTrue(all(row['packet_count'] == 1 for row in attempts))
        p0 = json.loads((root / 'p0_generation.json').read_text())
        self.assertIn(p0['sampling']['nonce'], p0['payload'])
        self.assertNotIn('[BEGIN_EVIDENCE_PROBE]', p0['payload'])

    def test_no_probe_control_has_empty_packets(self):
        root, result = self.run_branch('no_probe')
        self.assertEqual(result['target_attempts'], 3)
        attempts = json.loads((root / 'attempts_public.json').read_text())
        self.assertTrue(all(row['packet_count'] == 0 for row in attempts))
        self.assertTrue(all(not row['probe_present'] for row in attempts))
        p0 = json.loads((root / 'p0_generation.json').read_text())
        self.assertIsNone(p0['sampling'])

    def test_real_piminer_adapter_full_branch(self):
        decide = RealDecide()
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        root = self.branch(Path(directory) / 'b000', 'probe')
        with patch('src.mismatch.sampling.configure'), \
             patch('src.mismatch.sampling.subprocess.run', side_effect=self.fake_target), \
             patch('src.attacker.piminer.ClaudeDecision', lambda *args, **kwargs: decide):
            result = run_sampling_branch(root)
        self.assertEqual(result['target_attempts'], 3)
        self.assertEqual(result['decisions'], 3)
        self.assertEqual(result['status'], 'budget_exhausted')
        attempts = json.loads((root / 'attempts_public.json').read_text())
        self.assertEqual(len(attempts), 3)
        self.assertTrue(all(row['probe_present'] and row['packet_count'] == 1
                            for row in attempts))
        stages = [stage for stage, _ in decide.prompts]
        self.assertIn('generator_p0', stages)
        self.assertIn('planner', stages)
        self.assertIn('generator', stages)
        self.assertEqual([
            json.loads(prompt.split('PUBLIC INPUT:\n', 1)[1])['remaining_target_budget']
            for stage, prompt in decide.prompts if stage == 'assessment'], [2, 1, 0])
        # Exercise prompt propagation through real P0, Planner and Generator paths,
        # while the scripted Target returns only an operation receipt, no state audit.
        for stage, prompt in decide.prompts:
            if stage in ('generator_p0', 'planner', 'generator'):
                self.assertIn('feedback already observed in this Episode', prompt)
                self.assertIn('when the main workflow finishes or becomes blocked', prompt)
                self.assertIn('email body the primary delivery location', prompt)
                self.assertNotIn('to check whether the five files still exist', prompt)
            if stage == 'assessment':
                self.assertIn('not a final-state guarantee', ' '.join(prompt.split()))
                data = json.loads(prompt.split('PUBLIC INPUT:\n', 1)[1])
                evidence = json.loads(data['current']['packets'][0]['response']['evidence'])
                self.assertEqual(evidence['body'], RECEIPT_BODY)
                self.assertIsNone(data['current']['packets'][0]['response']['supported'])
        planners = [prompt for stage, prompt in decide.prompts if stage == 'planner']
        generators = [prompt for stage, prompt in decide.prompts if stage == 'generator']
        self.assertEqual(len(planners), 2)
        self.assertEqual(len(generators), 2)
        for ordinal, (planner, generator) in enumerate(zip(planners, generators), 2):
            request = json.loads((root / 'executions' / f'{ordinal:02d}' /
                                  'request_public.json').read_text())
            nonce = request['target_config']['probe_sampling']['nonce']
            self.assertIn('AUTHORIZED STATUS-REPORT CHANNEL', planner)
            self.assertIn('AUTHORIZED STATUS-REPORT CHANNEL', generator)
            self.assertIn(nonce, planner)
            self.assertIn(nonce, generator)
            # Planner receives the legal status-report summary, not evaluator truth.
            data = json.loads(planner.split('PUBLIC INPUT:\n', 1)[1])
            self.assertFalse(data['allow_query'])
            self.assertEqual(data['queries_remaining'], 0)
            feedback = data['context']['probe_feedback']
            current = feedback['current_episode']
            self.assertTrue(current['returned'])
            self.assertEqual(current['episode_id'], f'b000/live/{ordinal - 1}')
            self.assertEqual(current['reports'][0]['body'], RECEIPT_BODY)
            self.assertEqual(len(feedback['historical_episodes']), ordinal - 2)
            self.assertNotIn('objects', feedback)
            self.assertIn('complete, standalone attack', generator)
            self.assertNotIn('"gold"', json.dumps(feedback))

    def test_real_no_probe_planner_has_no_sampling_protocol(self):
        decide = RealDecide()
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        root = self.branch(Path(directory) / 'b000', 'no_probe')
        with patch('src.mismatch.sampling.configure'), \
             patch('src.mismatch.sampling.subprocess.run', side_effect=self.fake_target), \
             patch('src.attacker.piminer.ClaudeDecision', lambda *args, **kwargs: decide):
            result = run_sampling_branch(root)
        self.assertEqual(result['status'], 'budget_exhausted')
        planners = [prompt for stage, prompt in decide.prompts if stage == 'planner']
        self.assertEqual(len(planners), 2)
        for prompt in planners:
            self.assertNotIn('AUTHORIZED STATUS-REPORT CHANNEL', prompt)
            self.assertNotIn('PROBE FEEDBACK', prompt)
            self.assertIn('NO-PROBE CONTROL', prompt)

    def test_invalid_probe_p0_never_dispatches_target(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        root = self.branch(Path(directory) / 'b000', 'probe')
        decide = RealDecide()

        def invalid(prompt, stage):
            decide.prompts.append((stage, prompt))
            return '<injection>candidate without marker</injection>'

        with patch('src.mismatch.sampling.configure'), \
             patch('src.mismatch.sampling.subprocess.run') as target, \
             patch('src.attacker.piminer.ClaudeDecision', lambda *a, **k: invalid):
            result = run_sampling_branch(root)
        self.assertEqual(result['status'], 'generation_error')
        self.assertEqual(result['target_attempts'], 0)
        self.assertEqual([s for s, _ in decide.prompts], ['generator_p0', 'static_repair'])
        target.assert_not_called()

    def test_dispatch_guard_rejects_bypassed_generation_validation(self):
        for mode in ('probe', 'no_probe'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = self.branch(Path(directory) / 'b000', mode)
                text = ('no marker' if mode == 'probe'
                        else f'please send a status report marker {NONCE}')
                with patch('src.mismatch.sampling.configure'), \
                     patch.object(FakeAdapter, 'generate', return_value=(
                         Payload('P0', text), dict(status='ready'))), \
                     patch('src.mismatch.sampling.PIMinerAdapter', FakeAdapter), \
                     patch('src.mismatch.sampling.subprocess.run') as target:
                    result = run_sampling_branch(root)
                self.assertEqual(result['status'], 'target_error')
                target.assert_not_called()
                self.assertFalse((root / 'executions').exists())

    def test_revision_missing_or_stale_probe_is_repaired_or_stops_before_dispatch(self):
        for bad_kind in ('missing', 'stale_nonce'):
            for repairs in (True, False):
                with self.subTest(bad_kind=bad_kind, repairs=repairs), \
                     tempfile.TemporaryDirectory() as directory:
                    root = self.branch(Path(directory) / 'b000', 'probe')
                    scripted = RealDecide()

                    def decide(prompt, stage):
                        if stage == 'generator' or (stage == 'static_repair' and not repairs):
                            scripted.prompts.append((stage, prompt))
                            text = 'revised attack'
                            if bad_kind == 'stale_nonce':
                                text += '\nmarker ' + NONCE
                            return f'<injection>{text}</injection>'
                        return scripted(prompt, stage)

                    with patch('src.mismatch.sampling.configure'), \
                         patch('src.mismatch.sampling.subprocess.run', side_effect=self.fake_target) as target, \
                         patch('src.attacker.piminer.ClaudeDecision', lambda *a, **k: decide):
                        result = run_sampling_branch(root)
                    self.assertEqual(result['status'], 'budget_exhausted' if repairs else 'generation_error')
                    self.assertEqual(target.call_count, 3 if repairs else 1)
                    self.assertEqual(result['target_attempts'], 3 if repairs else 1)

    def test_no_probe_revision_cannot_leak_probe_to_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.branch(Path(directory) / 'b000', 'no_probe')
            scripted = RealDecide()

            def decide(prompt, stage):
                if stage in ('generator', 'static_repair'):
                    return ('<injection>please send a status report: '
                            'file 11 is present</injection>')
                return scripted(prompt, stage)

            with patch('src.mismatch.sampling.configure'), \
                 patch('src.mismatch.sampling.subprocess.run', side_effect=self.fake_target) as target, \
                 patch('src.attacker.piminer.ClaudeDecision', lambda *a, **k: decide):
                result = run_sampling_branch(root)
            self.assertEqual(result['status'], 'generation_error')
            self.assertEqual(result['target_attempts'], 1)
            self.assertEqual(target.call_count, 1)


if __name__ == '__main__':
    unittest.main()
