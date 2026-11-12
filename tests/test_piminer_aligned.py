"""Offline integration witnesses for fixed initialization and aligned sessions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from src.attacker.aligned import PIMinerAlignedAdapter
from src.attacker.experience import RollingExperience
from src.attacker.piminer import GenerationError
from src.engine.config import ROOT, load_cases, load_config
from src.engine.runner import Runner
from src.engine.storage import RunStore
from src.policies.always_retry import AlwaysRetry
from src.policies.always_revise import AlwaysRevise
from src.policies.fixed_schedule import FixedSchedule
from src.schema import TargetResult


BUNDLE = 'data/piminer_aligned_20261030/initializations.json'


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class FakeSession:
    def __init__(self, factory, options):
        self.factory = factory
        self.options = options
        self.session_dir = Path(options['session_dir'])
        self.session_id = f'offline-session-{len(factory.sessions)}'
        self.calls, self.prompts = [], []
        self.started = self.failed = False

    def __call__(self, prompt, stage):
        record = dict(stage=stage, session_id=self.session_id, resumed=self.started,
                      status='running', seconds=0, usage=None, model_usage=None)
        self.calls.append(record)
        self.prompts.append(prompt)
        ordinal = len(self.calls)
        if self.failed or ordinal == self.factory.fail_at:
            self.failed = True
            record.update(status='error', error_type='RuntimeError')
            raise RuntimeError('Offline simulated session failure')
        self.started = True
        record['status'] = 'completed'
        return (f'<analysis>analysis-{self.session_id}-{ordinal}</analysis>'
                f'<injection>candidate-{self.session_id}-{ordinal}</injection>')


class FakeFactory:
    def __init__(self, fail_at=None):
        self.sessions = []
        self.fail_at = fail_at

    def __call__(self, **options):
        session = FakeSession(self, options)
        self.sessions.append(session)
        return session


class FakeTarget:
    def __init__(self, *, gold=False, error=None):
        self.calls = []
        self.gold, self.error = gold, error

    def execute(self, case, payload, episode_id):
        self.calls.append(dict(case_id=case['case_id'], episode_id=episode_id,
                               payload_hash=payload.sha256))
        if self.error:
            return TargetResult(None, error=self.error)
        gold = self.gold(case, episode_id) if callable(self.gold) else self.gold
        return TargetResult(gold, utility=True, final='PRIVATE_FULL_FINAL',
                           black_box_final='PUBLIC_FINAL::'+episode_id,
                           raw={'private_state': 'PRIVATE_RAW_STATE'})


class PIMinerAlignedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases(load_config(ROOT/'experiments/evidence_piminer_test_asr5_20261030.yaml'))
        cls.initials = {entry['case_id']: entry for entry in
                        json.loads((ROOT/BUNDLE).read_text())['initializations']}
        if len(cls.cases) != 22:
            raise AssertionError('Expected the real 22-case observable benchmark fixture')

    def adapter(self, factory=None):
        return PIMinerAlignedAdapter(ROOT, model='offline', target_model='gpt-5-nano',
            initialization_manifest=BUNDLE, session_factory=factory or FakeFactory(),
            effort='max', timeout=900)

    def runner(self, path, adapter, target, policies, budget=4, parent=None):
        manifest = {'parent_run': str(parent)} if parent is not None else {}
        return Runner(target, adapter, SimpleNamespace(collect=lambda *args: ()), policies,
                      RunStore(path, manifest), post_failure_budget=budget, seed=20261030)

    def public_history(self, payload, analysis, index):
        return dict(iter=index, analysis=analysis, injection=payload.text, security=False,
                    utility=True, final_output=f'PUBLIC_ATTEMPT_{index}',
                    trajectory='PRIVATE_TRAJECTORY', evidence='PRIVATE_EVIDENCE')

    def assert_no_private(self, prompt):
        for marker in ('PRIVATE_FULL_FINAL', 'PRIVATE_RAW_STATE', 'PRIVATE_TRAJECTORY',
                       'PRIVATE_EVIDENCE', 'PRIVATE_CASE_STATE'):
            self.assertFalse(marker in prompt, 'Private marker entered the attacker prompt: '+marker)

    def test_all22_import_ordered_route_payload_and_analysis_with_fresh_target_no_model_calls(self):
        factory, target = FakeFactory(), FakeTarget()
        adapter = self.adapter(factory)
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(Path(directory)/'run', adapter, target,
                                 {'always_retry': AlwaysRetry()}, budget=0)
            cases = deepcopy(self.cases)
            # An arbitrary cached value on a case must never replace the audited import.
            for case in cases:
                case['p0'] = 'UNTRUSTED_CACHED_CANDIDATE'
            runner.run(cases)
            self.assertFalse(adapter.use_cached_p0)
            self.assertEqual(len(target.calls), 22)
            self.assertEqual(len(runner.episodes), 22)
            self.assertFalse(factory.sessions)
            self.assertTrue(all(outcome['initial_gold'] is False for outcome in runner.outcomes))
            self.assertEqual(len(runner.generations), 22)
            for generation, call in zip(runner.generations, target.calls):
                expected = self.initials[generation['case_id']]
                self.assertEqual(generation['routing']['strategy_ids'], expected['strategy_ids'])
                self.assertEqual(generation['payload_sha256'], expected['payload_sha256'])
                self.assertEqual(digest(generation['analysis']), expected['analysis_sha256'])
                self.assertEqual(call['payload_hash'], expected['payload_sha256'])
                self.assertFalse(generation['calls'])
                self.assertFalse(generation['reused_target_feedback'])
                self.assertEqual(call['episode_id'], generation['case_id']+'/shared/000')

    def test_runner_reuses_one_session_full_then_delta_preserves_analysis_and_caps_total_five(self):
        factory, target = FakeFactory(), FakeTarget()
        adapter = self.adapter(factory)
        case = deepcopy(self.cases[0])
        case['private_state'] = 'PRIVATE_CASE_STATE'
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(Path(directory)/'run', adapter, target,
                                 {'always_revise': AlwaysRevise()})
            runner.run([case])
            self.assertEqual(len(target.calls), 5)
            self.assertEqual(len(factory.sessions), 1)
            session = factory.sessions[0]
            self.assertEqual(len(session.calls), 4)
            revisions = runner.generations[1:]
            self.assertEqual([record['prompt_mode'] for record in revisions], ['full', 'delta', 'delta', 'delta'])
            self.assertEqual({record['session_id'] for record in revisions}, {session.session_id})
            self.assertEqual([record['new_history_iters'] for record in revisions], [[0], [1], [2], [3]])
            self.assertEqual([call['resumed'] for call in session.calls], [False, True, True, True])
            self.assertTrue(self.initials[case['case_id']]['analysis'] in session.prompts[0])
            document = adapter.bundle.strategy_document(case)
            self.assertTrue(document in session.prompts[0])
            for prompt in session.prompts[1:]:
                self.assertFalse(document in prompt)
            for prompt in session.prompts:
                self.assert_no_private(prompt)
            self.assertEqual(runner.outcomes[0]['target_attempts'], 4)
            self.assertIs(runner.outcomes[0]['gold_success'], False)
            _, metadata = RollingExperience(Path(directory)/'run'/'experience'/'always_revise').snapshot('c999')
            self.assertEqual(metadata['case_ids'], [case['case_id']])

    def test_retry_before_first_revision_is_in_full_prompt_and_later_delta_includes_all_unseen(self):
        factory = FakeFactory()
        adapter = self.adapter(factory)
        case = deepcopy(self.cases[0])
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory)/'run', {})
            adapter.bind_run(store.path, ['fixed_schedule'])
            p0, initial = adapter.generate(case, None, None, (), 'P0')
            adapter.begin_branch(case, 'fixed_schedule')
            history = [self.public_history(p0, initial['analysis'], i) for i in range(3)]
            failed = SimpleNamespace(gold=False, error=None)
            p1, first = adapter.generate_with_history(case, p0, failed, ('PRIVATE_EVIDENCE',), 'P1', history)
            self.assertEqual(first['prompt_mode'], 'full')
            self.assertEqual(first['new_history_iters'], [0, 1, 2])
            for i in range(3):
                self.assertEqual(factory.sessions[0].prompts[0].count(f'PUBLIC_ATTEMPT_{i}'), 1)
            for i in range(3, 6):
                history.append(self.public_history(p1, first['analysis'], i))
            _, second = adapter.generate_with_history(case, p1, failed, ('PRIVATE_EVIDENCE',), 'P2', history)
            self.assertEqual(second['prompt_mode'], 'delta')
            self.assertEqual(second['history_iters'], list(range(6)))
            self.assertEqual(second['new_history_iters'], [3, 4, 5])
            self.assertEqual(first['session_id'], second['session_id'])
            prompt = factory.sessions[0].prompts[1]
            for i in range(3):
                self.assertFalse(f'PUBLIC_ATTEMPT_{i}' in prompt)
            for i in range(3, 6):
                self.assertEqual(prompt.count(f'PUBLIC_ATTEMPT_{i}'), 1)
            for prompt in factory.sessions[0].prompts:
                self.assert_no_private(prompt)

    def test_runner_retries_keep_payload_and_first_revision_sees_all_completed_retries(self):
        factory, target = FakeFactory(), FakeTarget()
        adapter = self.adapter(factory)
        case = deepcopy(self.cases[0])
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(Path(directory)/'run', adapter, target,
                {'fixed_schedule': FixedSchedule(['Retry', 'Retry', 'Revise', 'Retry'])})
            runner.run([case])
            self.assertEqual(len(target.calls), 5)
            self.assertEqual(len(factory.sessions), 1)
            self.assertEqual(len(factory.sessions[0].calls), 1)
            self.assertEqual(len({call['payload_hash'] for call in target.calls[:3]}), 1)
            self.assertEqual(target.calls[3]['payload_hash'], target.calls[4]['payload_hash'])
            revision = runner.generations[1]
            self.assertEqual(revision['history_iters'], [0, 1, 2])
            self.assertEqual(revision['new_history_iters'], [0, 1, 2])
            prompt = factory.sessions[0].prompts[0]
            for call in target.calls[:3]:
                self.assertTrue('PUBLIC_FINAL::'+call['episode_id'] in prompt)

    def test_different_cases_and_policies_have_separate_sessions_and_experience(self):
        factory, target = FakeFactory(), FakeTarget()
        adapter = self.adapter(factory)
        cases = deepcopy(self.cases[:2])
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(Path(directory)/'run', adapter, target,
                {'arm_a': AlwaysRevise(), 'arm_b': AlwaysRevise()}, budget=1)
            runner.run(cases)
            self.assertEqual(len(factory.sessions), 4)
            self.assertEqual(len({session.session_id for session in factory.sessions}), 4)
            self.assertEqual(len({session.session_dir for session in factory.sessions}), 4)
            generations = {(g['case_id'], g['policy_id']): g for g in runner.generations if g['policy_id'] != 'shared'}
            for policy, other in [('arm_a', 'arm_b'), ('arm_b', 'arm_a')]:
                first = generations[cases[0]['case_id'], policy]
                second = generations[cases[1]['case_id'], policy]
                self.assertEqual(first['experience']['entries'], 0)
                self.assertEqual(second['experience']['case_ids'], [cases[0]['case_id']])
                self.assertEqual(second['experience']['policy_id'], policy)
                prompt = second['calls'][0]['prompt']
                self.assertTrue(first['payload'] in prompt)
                self.assertFalse(generations[cases[0]['case_id'], other]['payload'] in prompt)
                self.assertFalse('/'+other+'/' in prompt)
                self.assert_no_private(prompt)

    def test_separate_child_runners_share_only_terminal_same_policy_memory_and_refresh_each_generation(self):
        cases = deepcopy(self.cases[:3])
        factory_a = FakeFactory()
        adapter_a = self.adapter(factory_a)
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)/'parent'
            parent.mkdir()
            (parent/'manifest.json').write_text('{}\n')
            self.runner(parent/cases[0]['case_id'], adapter_a, FakeTarget(),
                        {'always_revise': AlwaysRevise()}, parent=parent)
            p0, initial = adapter_a.generate(cases[0], None, None, (), 'P0')
            adapter_a.begin_branch(cases[0], 'always_revise')
            history = [self.public_history(p0, initial['analysis'], 0)]
            failed = SimpleNamespace(gold=False, error=None)
            p1, before = adapter_a.generate_with_history(cases[0], p0, failed, (), 'P1', history)
            self.assertEqual(before['experience']['entries'], 0)
            # Independent child adapters publish through real Runner terminal hooks.
            adapter_b = self.adapter(FakeFactory())
            runner_b = self.runner(parent/cases[1]['case_id'], adapter_b, FakeTarget(gold=True),
                                  {'always_revise': AlwaysRevise()}, parent=parent)
            runner_b.run([cases[1]])
            adapter_c = self.adapter(FakeFactory())
            runner_c = self.runner(parent/cases[2]['case_id'], adapter_c, FakeTarget(gold=True),
                                  {'other_policy': AlwaysRevise()}, parent=parent)
            runner_c.run([cases[2]])
            history.append(self.public_history(p1, before['analysis'], 1))
            _, after = adapter_a.generate_with_history(cases[0], p1, failed, (), 'P2', history)
            self.assertEqual(after['prompt_mode'], 'delta')
            self.assertEqual(after['experience']['case_ids'], [cases[1]['case_id']])
            self.assertEqual(before['session_id'], after['session_id'])
            self.assertEqual(after['experience']['source'], str(parent/'experience'/'always_revise'/'intra_dataset_experience.jsonl'))
            text, metadata = RollingExperience(parent/'experience'/'always_revise').snapshot(cases[1]['case_id'])
            self.assertEqual(text, '')
            self.assertEqual(metadata['entries'], 0)
            self.assertFalse(cases[0]['case_id'] in after['experience']['case_ids'])
            self.assertFalse(cases[2]['case_id'] in after['experience']['case_ids'])

    def test_generation_failure_stops_target_without_publishing_or_replacing_session(self):
        factory, target = FakeFactory(fail_at=2), FakeTarget()
        adapter = self.adapter(factory)
        case = deepcopy(self.cases[0])
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(Path(directory)/'run', adapter, target,
                                 {'always_revise': AlwaysRevise()})
            runner.run([case])
            self.assertEqual(len(target.calls), 2)
            self.assertEqual(len(factory.sessions), 1)
            self.assertEqual(len(factory.sessions[0].calls), 2)
            self.assertEqual(runner.outcomes[0]['status'], 'GENERATION_ERROR')
            self.assertIsNone(runner.outcomes[0]['gold_success'])
            failed = runner.generations[-1]
            self.assertEqual(failed['status'], 'GENERATION_ERROR')
            self.assertEqual(failed['calls'][0]['provider']['error_type'], 'RuntimeError')
            self.assertEqual(failed['session_id'], runner.generations[-2]['session_id'])
            self.assertEqual(RollingExperience(Path(directory)/'run'/'experience'/'always_revise').snapshot('c999')[1]['entries'], 0)

    def test_target_error_and_identity_mismatch_never_publish_or_start_revision(self):
        for error, mutate, expected_calls, expected_status in [
                ('OfflineTransportError', False, 1, 'TARGET_ERROR'),
                (None, True, 0, 'GENERATION_ERROR')]:
            with self.subTest(error=error, identity_mutation=mutate), tempfile.TemporaryDirectory() as directory:
                factory, target = FakeFactory(), FakeTarget(error=error)
                adapter = self.adapter(factory)
                case = deepcopy(self.cases[0])
                if mutate:
                    case['user_task_prompt'] += ' deliberate mismatch'
                runner = self.runner(Path(directory)/'run', adapter, target,
                                     {'always_revise': AlwaysRevise()})
                runner.run([case])
                self.assertEqual(len(target.calls), expected_calls)
                self.assertFalse(factory.sessions)
                self.assertEqual(runner.outcomes[0]['status'], expected_status)
                self.assertIsNone(runner.outcomes[0]['gold_success'])
                self.assertEqual(RollingExperience(Path(directory)/'run'/'experience'/'always_revise').snapshot('c999')[1]['entries'], 0)

    def test_history_must_extend_imported_initial_analysis_and_current_branch(self):
        case = deepcopy(self.cases[0])
        factory = FakeFactory()
        adapter = self.adapter(factory)
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory)/'run', {})
            adapter.bind_run(store.path, ['always_revise'])
            p0, initial = adapter.generate(case, None, None, (), 'P0')
            adapter.begin_branch(case, 'always_revise')
            history = [self.public_history(p0, initial['analysis'], 0)]
            failed = SimpleNamespace(gold=False, error=None)
            wrong = deepcopy(history)
            wrong[0]['analysis'] = 'WRONG_IMPORTED_ANALYSIS'
            with self.assertRaises(GenerationError):
                adapter.generate_with_history(case, p0, failed, (), 'P1', wrong)
            self.assertFalse(factory.sessions)
            p1, first = adapter.generate_with_history(case, p0, failed, (), 'P1', history)
            extended = deepcopy(history)+[self.public_history(p1, first['analysis'], 1)]
            extended[0]['final_output'] = 'MUTATED_PRIOR_FEEDBACK'
            with self.assertRaises(GenerationError):
                adapter.generate_with_history(case, p1, failed, (), 'P2', extended)
            self.assertEqual(len(factory.sessions[0].calls), 1)


if __name__ == '__main__':
    unittest.main()
