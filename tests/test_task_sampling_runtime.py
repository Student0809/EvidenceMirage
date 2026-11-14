"""Offline campaign -> native tools -> granted events -> public feedback tests."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from src.evidence.task_sampling import load_profiles, marker, public_packets, validate_public_packets, profile_policy
from src.feedback.ablation import AssessmentModules
from src.mismatch.data import ROOT, write_json
from src.mismatch.online import target_job, live_packets
from src.mismatch.sampling import run_sampling_branch
from src.mismatch.task_sampling import prepare_campaign, validate_sampling, ablation_metadata
from src.mismatch.tasks import MismatchTargetAdapter
from src.schema import Payload
from tests.test_probe_sampling_campaign import FakeAdapter

PROFILES = ROOT / 'configs/mismatch/probe_sampling_all18_profiles.json'
SELECTION = ROOT / 'configs/mismatch/all18_m2.json'
PREPARED = ROOT / 'data/mismatch_autonomous_20261104'


def case(sid):
    from src.feedback.schema import public_case
    return public_case(json.loads((ROOT / f'reports/mismatch_p0_20261104/inputs/{sid}.json').read_text()))


def pipeline(profile):
    def query(prompt, runtime, env):
        route, receiver = profile['route'], profile['receiver']
        if route == 'email_report':
            name, args = 'send_email', dict(recipients=[receiver], subject='Operation feedback',
                                           body=marker(profile['nonce']) + ' Already seen: one completed call. Unknown final state.')
        elif route == 'slack_report':
            name, args = 'send_direct_message', dict(recipient=receiver, body=marker(profile['nonce']) + ' Feedback already seen.')
        elif route == 'payment_events':
            name, args = 'send_money', dict(recipient=receiver, amount=1.0, subject='original task memo', date='2024-05-15')
        else:
            name = None
        if name:
            _, error = runtime.run_function(env, name, args)
            if error:
                raise AssertionError(error)
        messages = [dict(role='assistant', content=[dict(type='text', content='Operations already seen; current final state is unknown.')], tool_calls=[])]
        return prompt, runtime, env, messages, {}
    return SimpleNamespace(query=query)


class ScriptedAttacker(FakeAdapter):
    def generate(self, *args, sampling=None, **kwargs):
        self.decide('p0', 'generator_p0')
        text = 'Complete the original goal.'
        if sampling and sampling.config['route'] in ('email_report', 'slack_report'):
            text += ' Report to ' + sampling.config['receiver'] + ' ' + marker(sampling.nonce)
        return Payload('P0', text), dict(status='ready', analysis='')

    def revise(self, attempt, history, decision, *, feedback_view, sampling=None):
        payload, record = self.generate(sampling=sampling)
        return Payload('P' + str(len(history)), payload.text), record


class TaskRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profiles = load_profiles(PROFILES)

    def test_native_all18_routes_and_explicit_grants(self):
        for sid, template in self.profiles.items():
            with self.subTest(seed=sid):
                profile = dict(template, nonce='a' * 16)
                value = case(sid)
                value['resource_grants'] = dict(template['resource_grants'], mailboxes=['stolen@example.org'])
                adapter = MismatchTargetAdapter(task_feedback=profile, pipeline=pipeline(profile))
                result = adapter.execute(value, Payload('P0', 'fixture'), sid)
                self.assertIsNone(result.error, result.raw)
                from src.vendor.attacker_observer import fingerprint
                self.assertEqual(result.observation['policy'], fingerprint(profile_policy(profile)))
                report = result.raw['task_feedback']
                packets = public_packets(report)
                validate_public_packets(packets, profile, sid)
                self.assertEqual(len(packets), 0 if profile['route'] == 'final_report' else 1)
                if profile['route'] == 'final_report':
                    self.assertEqual(report['diagnostics']['delivery_status'], 'not_applicable')
                with self.assertRaises(ValueError):
                    live_packets(dict(raw=result.raw), profile['suite'])

    def test_observation_failure_does_not_erase_goal_score(self):
        profile = dict(self.profiles['S06'], nonce='a' * 16)
        adapter = MismatchTargetAdapter(task_feedback=profile, pipeline=pipeline(profile))
        expected = adapter.execute(case('S06'), Payload('P0', 'fixture'), 'baseline')
        with patch('src.adapters.agentdojo.Collector.finish', side_effect=ValueError('fixture')):
            result = adapter.execute(case('S06'), Payload('P0', 'fixture'), 'failed')
        self.assertIsNone(result.error)
        self.assertEqual(result.gold, expected.gold)
        self.assertEqual(result.observation_error, 'ValueError')
        self.assertEqual(public_packets(result.raw['task_feedback']), [])

    def test_profile_cannot_be_rebound_to_another_seed(self):
        profile = dict(self.profiles['S01'], nonce='a' * 16)
        result = MismatchTargetAdapter(task_feedback=profile, pipeline=pipeline(profile)).execute(
            case('S02'), Payload('P0', 'fixture'), 'wrong')
        self.assertEqual(result.error, 'ValueError')

    def test_invalid_projection_is_rejected_before_target_execution(self):
        profile = dict(self.profiles['S01'], nonce='a' * 16)
        for projection, final in (('legacy_write_receipts', 'text_only'),
                                  ('task_feedback', 'black_box'), ('none', None)):
            with self.subTest(projection=projection, final=final), tempfile.TemporaryDirectory() as directory:
                request = dict(case=case('S01'), payload=dict(id='P0', text='fixture'),
                               target_config=dict(task_feedback=profile), episode_id='fixture',
                               feedback_projection=projection, final_projection=final)
                path = Path(directory) / 'request_public.json'
                write_json(path, request)
                with patch('src.mismatch.online.configure'), patch('src.mismatch.tasks.MismatchTargetAdapter') as adapter:
                    with self.assertRaises(ValueError):
                        target_job(path)
                adapter.assert_not_called()
        with self.assertRaises(ValueError):
            MismatchTargetAdapter(task_feedback=self.profiles['S01'], pipeline=pipeline(profile))

    def args(self, output, mode='probe', **changes):
        values = dict(prepared=PREPARED, selection=SELECTION, profiles=PROFILES,
                      output=output, mode=mode, ablation='G11', feedback_visibility=None,
                      repeats=1, workers=1, dry_run=False)
        values.update(changes)
        return SimpleNamespace(**values)

    def test_prepare_all18_and_independent_switches(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, jobs = prepare_campaign(self.args(Path(directory) / 'run', repeats=2))
            self.assertEqual(len(jobs), 36)
            self.assertEqual(manifest['effort'], 'low')
            self.assertIsNone(manifest['target']['reasoning_effort'])
            self.assertEqual(manifest['max_target_dispatches'], 108)
            self.assertEqual({row['repeat'] for row in json.loads((jobs[0].parents[1] / 'selection_private.json').read_text())}, {1, 2})
            for job_path in jobs:
                job = json.loads((job_path / 'job_public.json').read_text())
                self.assertEqual(job['effort'], manifest['effort'])
                self.assertEqual(set(job['input']), {'case', 'strategy_document'})
                self.assertEqual(job['profile'], job['target_config']['task_feedback'])
                self.assertNotIn('nonce', job['profile'])
            for mode in ('probe', 'no_probe'):
                for visibility in ('visible', 'hidden'):
                    args = self.args(Path(directory) / f'{mode}-{visibility}', mode,
                                     feedback_visibility=visibility, dry_run=True)
                    validation, jobs = prepare_campaign(args)
                    self.assertEqual(jobs, [])
                    self.assertEqual(validation['api_calls'], 0)
                    self.assertEqual(validation['effort'], 'low')
                    self.assertEqual(validation['max_target_dispatches'], 54)
                    self.assertEqual(validation['ablation']['historical_experience'], True)
                    self.assertEqual(validation['ablation']['feedback_visibility'], visibility)
                    self.assertFalse(args.output.exists())

    def test_explicit_attacker_effort_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(Path(directory) / 'run', attacker_effort='max')
            validation, _ = prepare_campaign(SimpleNamespace(**dict(vars(args), dry_run=True)))
            manifest, jobs = prepare_campaign(args)
            self.assertEqual(validation['effort'], 'max')
            self.assertEqual(manifest['effort'], 'max')
            for path in jobs:
                job = json.loads((path / 'job_public.json').read_text())
                self.assertEqual(job['effort'], 'max')
                self.assertEqual(job['target_config']['model'], 'gpt-5-nano')
                self.assertIsNone(job['target_config']['reasoning_effort'])

    def test_four_routes_full_branch_and_hidden_control(self):
        def native_adapter(**kwargs):
            return MismatchTargetAdapter(pipeline=pipeline(kwargs['task_feedback']), **kwargs)
        def dispatch(command, **kwargs):
            with patch('src.mismatch.online.configure'), patch('src.mismatch.tasks.MismatchTargetAdapter', side_effect=native_adapter):
                target_job(command[-1])
        with tempfile.TemporaryDirectory() as directory:
            for mode in ('probe', 'no_probe'):
                _, jobs = prepare_campaign(self.args(Path(directory) / mode, mode))
                for index in (0, 3, 5, 6):
                    with self.subTest(mode=mode, index=index):
                        with patch('src.mismatch.sampling.configure'), patch('src.mismatch.sampling.PIMinerAdapter', ScriptedAttacker), patch('src.mismatch.sampling.subprocess.run', side_effect=dispatch):
                            result = run_sampling_branch(jobs[index])
                        self.assertEqual(result['status'], 'self_reported_complete', result)
                        self.assertEqual(result['target_attempts'], 3)
                        rows = json.loads((jobs[index] / 'attempts_public.json').read_text())
                        self.assertEqual(len({row['episode_id'] for row in rows}), 3)
                        for row in rows:
                            self.assertEqual(bool(row['packets']), mode == 'probe' and index != 6)
                        self.assertTrue((jobs[index] / 'frozen.json').exists())


if __name__ == '__main__':
    unittest.main()
