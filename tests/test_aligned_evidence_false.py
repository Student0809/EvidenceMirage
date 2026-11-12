"""Offline witnesses for evidence-F revisions and isolated PIMiner-U sessions.

The only replay fixture is an already stored, authorized banking observation.
Later episode IDs below represent local fake executions, never new API results.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from src.attacker.aligned import PIMinerAlignedAdapter
from src.attacker.experience import RollingExperience
from src.attacker.piminer import GenerationError
from src.engine.config import ROOT, load_cases, load_config
from src.engine.storage import RunStore
from src.engine.runner import Runner
from src.evidence.ledger import EvidenceLedger
from src.policies.stage_aware import StageAware
from src.schema import TargetResult
from src.vendor.revision_checker import SEMANTIC_DIMENSIONS
from tests.test_piminer_aligned import FakeFactory, FakeSession


BUNDLE = 'data/piminer_aligned_20261030/initializations.json'
RETRIEVAL = 'data/a2/aligned22_20261030.field_v2.retrieval.json'
REPLAY = ('runs/evidence_piminer_aligned_asr5_20261030_recovered_v2/'
          'c004/raw/c004/shared/000.json')
EVIDENCE_ONLY = 'AUTHORIZED_F_EVIDENCE_ONLY_SENTINEL'
PLAN_ONLY = 'F_PLAN_ONLY_SENTINEL'
ANALYSIS_ONLY = 'F_GENERATOR_ANALYSIS_ONLY_SENTINEL'
POLICY = 'stage_aware'


class EvidenceDecision:
    """A tool-free fake with the real planner and checker schemas."""

    def __init__(self, goal, candidates=(), fail_stage=None):
        self.goal, self.candidates, self.fail_stage = goal, list(candidates), fail_stage
        self.calls = []
        self.generated = 0

    def __call__(self, prompt, stage):
        item = dict(stage=stage, prompt=prompt, status='running', seconds=0)
        self.calls.append(item)
        if stage == self.fail_stage:
            item.update(status='error', error_type='RuntimeError')
            raise RuntimeError('Offline grounded failure')
        item['status'] = 'completed'
        if stage == 'preflight':
            return 'OK'
        if stage == 'planner':
            return json.dumps(dict(action='make_plan', observations=['goal'],
                hypothesis=PLAN_ONLY, edit_scope='Clarify one bounded instruction',
                preserve=[], unknowns=['No private action or causal failure is established'],
                cause='unknown', strategy_rationale='Retain the selected strategy with a bounded clarification.',
                hypothesis_test=dict(support_refs=['goal'], counter_refs=[],
                    unknowns=['The causal explanation remains unknown'],
                    predicted_observation='The authorized receiver event may appear.'),
                objective=self.goal, edits=[dict(change='Clarify the intended recipient',
                    basis='hypothesis', references=['condition:payment_received', 'goal'],
                    expected_effect='Test a bounded recipient clarification')]))
        if stage == 'semantic_review':
            return dict(assessments={k:'unknown' for k in SEMANTIC_DIMENSIONS}, findings=[])
        if stage not in ('generator', 'static_repair'):
            raise AssertionError('Unexpected grounded stage: ' + stage)
        self.generated += 1
        if self.candidates:
            return self.candidates.pop(0)
        return (f'<analysis>{ANALYSIS_ONLY}</analysis>'
                f'<injection>F_CANDIDATE_{self.generated}</injection>')


class NeutralSession(FakeSession):
    """Implement only the neutral continuity preflight locally."""

    def __call__(self, prompt, stage):
        if not stage.startswith('preflight_'):
            return super().__call__(prompt, stage)
        self.prompts.append(prompt)
        self.calls.append(dict(stage=stage, session_id=self.session_id,
                               resumed=self.started, status='completed', seconds=0))
        self.started = True
        if stage.endswith('_start'):
            self.nonce = prompt.split('Remember this token for our next turn: ', 1)[1].split('.', 1)[0]
            return 'OK'
        return self.nonce


class NeutralFactory(FakeFactory):
    def __call__(self, **options):
        session = NeutralSession(self, options)
        self.sessions.append(session)
        return session


class AlignedEvidenceFalseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cases = load_cases(load_config(ROOT / 'experiments/evidence_piminer_test_asr5_20261030.yaml'))
        cls.case = next(case for case in cases if case['case_id'] == 'c004')
        replay = json.loads((ROOT / REPLAY).read_text())
        cls.packet_fixture = replay['query_packets']
        ledger = EvidenceLedger('c004/shared/000', cls.case['goal_spec'])
        for packet in cls.packet_fixture:
            ledger.update(packet)
        if ledger.trigger != 'F' or replay['result']['gold'] is not False:
            raise AssertionError('Expected the recorded native banking False fixture')

    def adapter(self, factory, decide, mode='grounded'):
        return PIMinerAlignedAdapter(ROOT, model='offline', target_model='gpt-5-nano',
            initialization_manifest=BUNDLE, session_factory=factory,
            a2_library='strategy_library_a2/library.json', a2_retrieval=RETRIEVAL,
            false_revision_mode=mode, evidence_decide=decide)

    @contextmanager
    def branch(self, *, factory=None, decide=None, mode='grounded'):
        factory = factory or FakeFactory()
        decide = decide or EvidenceDecision(self.case['injection_task_goal'])
        adapter = self.adapter(factory, decide, mode)
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / 'run', {})
            adapter.bind_run(store.path, [POLICY])
            p0, initial = adapter.generate(deepcopy(self.case), None, None, (), 'P0')
            adapter.begin_branch(self.case, POLICY)
            history = [self.attempt(p0, initial['analysis'], 0)]
            yield adapter, factory, decide, p0, history, store.path

    @staticmethod
    def attempt(payload, analysis, index):
        return dict(iter=index, analysis=analysis, injection=payload.text,
            security=False, utility=True, final_output=f'PUBLIC_FEEDBACK_{index}',
            trajectory='PRIVATE_TARGET_TRAJECTORY', raw='PRIVATE_RAW_STATE',
            evidence='PRIVATE_HISTORY_EVIDENCE')

    def failure(self, index, trigger='F', **updates):
        eid = 'c004/shared/000' if index == 0 else f'c004/{POLICY}/{index:03d}'
        values = dict(episode_id=eid, case_id='c004', gold=False, error=None,
            trigger=trigger, final='PRIVATE_EPISODE_FINAL', raw='PRIVATE_RAW_STATE')
        values.update(updates)
        return SimpleNamespace(**values)

    def packets(self, episode):
        packets = deepcopy(self.packet_fixture)
        for packet in packets:
            packet['episode_id'] = episode.episode_id
            body = json.loads(packet['response']['evidence'])
            body['interpretation'] = EVIDENCE_ONLY
            packet['response']['evidence'] = json.dumps(body)
        return packets

    def assert_black_box(self, prompt):
        for forbidden in (EVIDENCE_ONLY, PLAN_ONLY, ANALYSIS_ONLY, 'PRIVATE_EPISODE_FINAL',
                          'PRIVATE_TARGET_TRAJECTORY', 'PRIVATE_RAW_STATE',
                          'PRIVATE_HISTORY_EVIDENCE', 'PRIVATE_PACKET_METADATA'):
            self.assertNotIn(forbidden, prompt)

    def revise(self, adapter, payload, history, number, trigger='F', packets=None):
        episode = self.failure(len(history) - 1, trigger)
        if packets is None:
            packets = self.packets(episode) if trigger == 'F' else ()
        return adapter.generate_with_history(self.case, payload, episode, packets,
                                             f'P{number}', history)

    def test_false_uses_real_planner_checker_a2_document_and_projected_final(self):
        with self.branch() as (adapter, factory, decide, p0, history, _):
            p1, record = self.revise(adapter, p0, history, 1)
            self.assertEqual(p1.text, 'F_CANDIDATE_1')
            self.assertEqual(record['revision_path'], 'grounded_false')
            self.assertEqual(record['feedback_history_iters'], [0])
            self.assertEqual(record['analysis'], '')
            self.assertNotIn('session_id', record)
            self.assertEqual([c['stage'] for c in record['calls']], ['planner', 'generator', 'semantic_review'])
            self.assertEqual(len(record['checks']), 1)
            self.assertIn(record['checks'][0]['status'], ('PASS', 'REVIEW'))
            self.assertFalse(factory.sessions)
            self.assertFalse(adapter.seen_history)
            document = adapter.routes['c004']['document']
            self.assertNotEqual(document, adapter.bundle.strategy_document(self.case))
            self.assertIn(document, decide.calls[1]['prompt'])
            planner_input = json.loads(decide.calls[0]['prompt'].split('\nPUBLIC INPUT:\n', 1)[1])
            self.assertEqual(planner_input['context']['strategy_document'], document)
            self.assertEqual(record['revision_protocol'], 'grounded_history_plan_v2')
            self.assertEqual(record['revision_base_sha256'], p0.sha256)
            self.assertFalse(record['semantic_checks'][0]['interpretation_verified'])
            self.assertEqual(record['routing']['a2']['strategy_document_sha256'],
                             adapter.routes['c004']['a2']['strategy_document_sha256'])
            for call in decide.calls:
                self.assertIn('PUBLIC_FEEDBACK_0', call['prompt'])
                self.assertIn(EVIDENCE_ONLY, call['prompt'])
                self.assertNotIn('PRIVATE_EPISODE_FINAL', call['prompt'])
                self.assertNotIn('PRIVATE_TARGET_TRAJECTORY', call['prompt'])
                self.assertNotIn('PRIVATE_RAW_STATE', call['prompt'])
                self.assertNotIn('PRIVATE_HISTORY_EVIDENCE', call['prompt'])

    def test_false_unknown_false_has_one_black_box_session_and_no_plan_leakage(self):
        with self.branch() as (adapter, factory, decide, p0, history, _):
            p1, f1 = self.revise(adapter, p0, history, 1)
            history.append(self.attempt(p1, f1['analysis'], 1))
            p2, u1 = self.revise(adapter, p1, history, 2, 'U')
            self.assertEqual(u1['prompt_mode'], 'full')
            self.assertEqual(u1['new_history_iters'], [0, 1])
            self.assertEqual(len(factory.sessions), 1)
            self.assertEqual(len(factory.sessions[0].calls), 1)
            prompt = factory.sessions[0].prompts[0]
            self.assertIn(p1.text, prompt)
            self.assertIn('PUBLIC_FEEDBACK_1', prompt)
            self.assert_black_box(prompt)
            seen = deepcopy(adapter.seen_history[('c004', POLICY)])
            history.append(self.attempt(p2, u1['analysis'], 2))
            _, f2 = self.revise(adapter, p2, history, 3)
            self.assertEqual(f2['feedback_history_iters'], [0, 1, 2])
            self.assertEqual(adapter.seen_history[('c004', POLICY)], seen)
            self.assertEqual(len(factory.sessions[0].calls), 1)
            self.assertEqual(len(decide.calls), 6)
            self.assertIn('PUBLIC_FEEDBACK_1', decide.calls[-1]['prompt'])
            self.assertIn('PUBLIC_FEEDBACK_2', decide.calls[-1]['prompt'])

    def test_unknown_false_unknown_replays_every_unseen_black_box_attempt(self):
        with self.branch() as (adapter, factory, _, p0, history, _):
            p1, u1 = self.revise(adapter, p0, history, 1, 'U')
            history.append(self.attempt(p1, u1['analysis'], 1))
            p2, f1 = self.revise(adapter, p1, history, 2)
            history.append(self.attempt(p2, f1['analysis'], 2))
            _, u2 = self.revise(adapter, p2, history, 3, 'U')
            self.assertEqual(u2['prompt_mode'], 'delta')
            self.assertEqual(u2['new_history_iters'], [1, 2])
            self.assertEqual(u1['session_id'], u2['session_id'])
            self.assertEqual(len(factory.sessions), 1)
            prompts = factory.sessions[0].prompts
            self.assertEqual(len(prompts), 2)
            self.assertNotIn('PUBLIC_FEEDBACK_0', prompts[1])
            for marker in ('PUBLIC_FEEDBACK_1', 'PUBLIC_FEEDBACK_2', p1.text, p2.text):
                self.assertIn(marker, prompts[1])
            for prompt in prompts:
                self.assert_black_box(prompt)

    def test_prefix_cannot_be_changed_while_crossing_either_revision_path(self):
        for first, second in [('F', 'U'), ('U', 'F')]:
            with self.subTest(first=first), self.branch() as (adapter, factory, decide, p0, history, _):
                p1, record = self.revise(adapter, p0, history, 1, first)
                history.append(self.attempt(p1, record['analysis'], 1))
                history[0]['final_output'] = 'MUTATED_OLD_FEEDBACK'
                before = (len(decide.calls), sum(len(s.calls) for s in factory.sessions))
                with self.assertRaises(GenerationError):
                    self.revise(adapter, p1, history, 2, second)
                self.assertEqual(before, (len(decide.calls), sum(len(s.calls) for s in factory.sessions)))

    def test_forged_false_or_foreign_unauthorized_packets_make_no_model_call(self):
        def variants(episode):
            legal = self.packets(episode)
            yield 'missing', []
            foreign = deepcopy(legal); foreign[0]['episode_id'] = 'c999/shared/000'
            yield 'foreign_episode', foreign
            policy = deepcopy(legal); policy[0]['policy'] = '0' * 64
            yield 'wrong_grants', policy
            query = deepcopy(legal); query[0]['query_id'] = 'website_visited'
            yield 'unauthorized_query', query
            suite = deepcopy(legal); suite[0]['event_coverage']['suite_name'] = 'workspace'
            yield 'foreign_suite', suite
            unsupported = deepcopy(legal); unsupported[0]['response']['supported'] = True
            yield 'inconsistent_false', unsupported
            private = deepcopy(legal); private[0]['source'] = 'private_evaluator_state'
            yield 'private_source', private
            unknown = deepcopy(legal)
            unknown[0]['completeness'] = 'returned_event_fields_only'
            del unknown[0]['event_coverage']
            unknown[0]['response']['supported'] = None
            yield 'uncertified_absence', unknown

        for label, packets in variants(self.failure(0)):
            with self.subTest(label=label), self.branch() as (adapter, factory, decide, p0, history, _):
                with self.assertRaises(GenerationError):
                    self.revise(adapter, p0, history, 1, packets=packets)
                self.assertFalse(factory.sessions)
                self.assertFalse(decide.calls)
                self.assertFalse(adapter.generation_history)

    def test_packet_private_metadata_is_projected_out_before_grounded_calls(self):
        with self.branch() as (adapter, _, decide, p0, history, _):
            packets = self.packets(self.failure(0))
            packets[0]['raw'] = 'PRIVATE_PACKET_METADATA'
            packets[0]['response']['judge_state'] = 'PRIVATE_PACKET_METADATA'
            self.revise(adapter, p0, history, 1, packets=packets)
            for call in decide.calls:
                self.assertNotIn('PRIVATE_PACKET_METADATA', call['prompt'])

    def test_false_requires_current_case_valid_failure_and_string_projected_final(self):
        for updates, feedback in [({'case_id': 'c999'}, 'public'),
                ({'gold': True}, 'public'), ({'error': 'OfflineTargetError'}, 'public'),
                ({}, None)]:
            with self.subTest(updates=updates, feedback=feedback), self.branch() as (adapter, factory, decide, p0, history, _):
                history[-1]['final_output'] = feedback
                episode = self.failure(0, **updates)
                with self.assertRaises(GenerationError):
                    adapter.generate_with_history(self.case, p0, episode, self.packets(episode), 'P1', history)
                self.assertFalse(factory.sessions)
                self.assertFalse(decide.calls)

    def test_legacy_default_keeps_original_black_box_even_when_false_packets_present(self):
        factory, decide = FakeFactory(), EvidenceDecision(self.case['injection_task_goal'])
        adapter = PIMinerAlignedAdapter(ROOT, model='offline', target_model='gpt-5-nano',
            initialization_manifest=BUNDLE, session_factory=factory,
            evidence_decide=decide)
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / 'run', {})
            adapter.bind_run(store.path, [POLICY])
            p0, initial = adapter.generate(self.case, None, None, (), 'P0')
            adapter.begin_branch(self.case, POLICY)
            history = [self.attempt(p0, initial['analysis'], 0)]
            _, record = self.revise(adapter, p0, history, 1, packets=('PRIVATE_PACKET_METADATA',))
            self.assertEqual([c['stage'] for c in record['calls']], ['attacker_revise'])
            self.assertEqual(len(factory.sessions), 1)
            self.assertFalse(decide.calls)
            self.assert_black_box(factory.sessions[0].prompts[0])

    def test_preflight_adds_one_independent_grounded_call_to_two_neutral_turns(self):
        factory, decide = NeutralFactory(), EvidenceDecision(self.case['injection_task_goal'])
        result = self.adapter(factory, decide).preflight()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len(result['attacker']['calls']), 2)
        self.assertEqual([c['stage'] for c in result['grounded']['calls']], ['preflight'])
        self.assertEqual(len(factory.sessions), 1)
        self.assertEqual(len(decide.calls), 1)
        for prompt in factory.sessions[0].prompts:
            self.assert_black_box(prompt)

    def test_false_generator_gets_at_most_one_static_repair(self):
        for candidates, success in [(['invalid', '<injection>REPAIRED_F</injection>'], True),
                (['invalid', '<injection></injection>'], False)]:
            decide = EvidenceDecision(self.case['injection_task_goal'], candidates)
            with self.subTest(success=success), self.branch(decide=decide) as (adapter, factory, _, p0, history, _):
                if success:
                    result, record = self.revise(adapter, p0, history, 1)
                    self.assertEqual(result.text, 'REPAIRED_F')
                else:
                    with self.assertRaises(GenerationError) as caught:
                        self.revise(adapter, p0, history, 1)
                    record = caught.exception.record
                    self.assertFalse(adapter.generation_history)
                expected = ['planner', 'generator', 'static_repair']
                if success:
                    expected.append('semantic_review')
                self.assertEqual([c['stage'] for c in decide.calls], expected)
                self.assertEqual(len(record['checks']), 2)
                self.assertEqual(record['checks'][0]['status'], 'FAIL')
                self.assertEqual(record['analysis'], '')
                self.assertFalse(factory.sessions)
                self.assertFalse(adapter.seen_history)

    def test_false_model_failure_preserves_error_without_advancing_either_history(self):
        decide = EvidenceDecision(self.case['injection_task_goal'], fail_stage='generator')
        with self.branch(decide=decide) as (adapter, factory, _, p0, history, _):
            with self.assertRaises(GenerationError) as caught:
                self.revise(adapter, p0, history, 1)
            record = caught.exception.record
            self.assertEqual(record['error_type'], 'RuntimeError')
            self.assertEqual(record['status'], 'GENERATION_ERROR')
            self.assertEqual(record['calls'][-1]['status'], 'error')
            self.assertEqual(record['analysis'], '')
            self.assertFalse(adapter.generation_history)
            self.assertFalse(adapter.seen_history)
            self.assertFalse(factory.sessions)

    def test_runner_retains_positive_initial_then_false_retry_for_same_candidate(self):
        """Synthetic episode sequence through the actual Runner, not new results."""
        test = self

        class Target:
            def __init__(self):
                self.calls = []

            def execute(self, case, payload, episode_id):
                self.calls.append(payload.sha256)
                return TargetResult(False, final='PRIVATE_TARGET_FINAL',
                    black_box_final='PUBLIC_EXECUTION_' + episode_id)

        class Observer:
            calls = 0

            def collect(self, episode_id, case, view):
                packets = deepcopy(test.packet_fixture)
                for packet in packets:
                    packet['episode_id'] = episode_id
                    if self.calls == 0:
                        body = json.loads(packet['response']['evidence'])
                        body.update(observed_count=1, observed=[{'offline_fixture_event': True}])
                        packet['response'] = dict(supported=True, evidence=json.dumps(body))
                self.calls += 1
                return packets

        decide, factory, target = EvidenceDecision(self.case['injection_task_goal']), FakeFactory(), Target()
        adapter = self.adapter(factory, decide)
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(target, adapter, Observer(), {POLICY: StageAware(unknown_action='revise')},
                            RunStore(Path(directory) / 'run', {}), post_failure_budget=2)
            runner.run([deepcopy(self.case)])
        self.assertEqual([e.trigger for e in runner.episodes], ['T', 'F', 'F'])
        self.assertEqual([d['action'] for d in runner.decisions], ['Retry', 'Revise', 'Stop'])
        self.assertEqual(target.calls[0], target.calls[1])
        revision = next(g for g in runner.generations if g.get('revision_path') == 'grounded_false')
        memory = revision['revision_memory']
        self.assertEqual(revision['feedback_history_iters'], [0, 1])
        self.assertEqual(memory['candidates'][0]['trigger_counts'], dict(T=1, F=1, U=0))
        self.assertEqual(memory['positive_reference_candidates'], [target.calls[0]])
        self.assertEqual(memory['candidates'][0]['full_trigger_episode_ids'], ['c004/shared/000'])
        self.assertEqual(len(memory['attempts']), 2)
        self.assertEqual(revision['parent_payload_sha256'], target.calls[0])
        for call in decide.calls:
            self.assertIn('PUBLIC_EXECUTION_c004/shared/000', call['prompt'])
            self.assertIn('PUBLIC_EXECUTION_c004/stage_aware/001', call['prompt'])
            self.assertNotIn('PRIVATE_TARGET_FINAL', call['prompt'])
        self.assertFalse(factory.sessions)

    def test_anchored_semantic_conflict_requests_only_one_candidate_repair(self):
        class ConflictDecision(EvidenceDecision):
            def __call__(self, prompt, stage):
                result = super().__call__(prompt, stage)
                if stage == 'semantic_review' and self.generated == 1:
                    result['assessments']['plan_following'] = 'conflict'
                    result['findings'] = [dict(dimension='plan_following',
                        explanation='Offline fixture: the requested edit was not followed.',
                        quotes=[dict(source='candidate', text='F_CANDIDATE_1')])]
                return result

        decide = ConflictDecision(self.case['injection_task_goal'])
        with self.branch(decide=decide) as (adapter, _, _, p0, history, _):
            candidate, record = self.revise(adapter, p0, history, 1)
        self.assertEqual(candidate.text, 'F_CANDIDATE_2')
        self.assertEqual([c['stage'] for c in record['calls']],
                         ['planner', 'generator', 'semantic_review', 'static_repair', 'semantic_review'])
        self.assertEqual(len(record['checks']), 2)
        self.assertEqual(len(record['semantic_checks']), 2)
        self.assertIn('anchored advisory findings', record['calls'][3]['prompt'])

    def test_missing_advisory_review_coverage_is_recorded_without_inventing_failure(self):
        decide = EvidenceDecision(self.case['injection_task_goal'], fail_stage='semantic_review')
        with self.branch(decide=decide) as (adapter, _, _, p0, history, _):
            candidate, record = self.revise(adapter, p0, history, 1)
        self.assertEqual(candidate.text, 'F_CANDIDATE_1')
        self.assertEqual(record['status'], 'ready')
        self.assertEqual(record['semantic_checks'][0]['status'], 'unavailable')
        self.assertEqual(record['calls'][-1]['status'], 'error')

    def test_previous_grounded_evidence_cannot_be_mutated(self):
        with self.branch() as (adapter, _, decide, p0, history, _):
            p1, record = self.revise(adapter, p0, history, 1)
            old = deepcopy(record['revision_memory']['attempts'][0]['revision_observation'])
            old['query_results'][0]['response']['evidence'] = 'MUTATED_PRIOR_EVIDENCE'
            history[0]['revision_observation'] = old
            history.append(self.attempt(p1, record['analysis'], 1))
            before = len(decide.calls)
            with self.assertRaises(GenerationError):
                self.revise(adapter, p1, history, 2)
            self.assertEqual(len(decide.calls), before)

    def test_committed_memory_contains_candidate_and_feedback_without_false_plan_analysis(self):
        with self.branch() as (adapter, _, _, p0, history, path):
            p1, record = self.revise(adapter, p0, history, 1)
            history.append(self.attempt(p1, record['analysis'], 1))
            outcome = dict(case_id='c004', policy_id=POLICY, gold_success=False,
                           status='VALID_GOLD_FALSE', target_attempts=1)
            self.assertTrue(adapter.finish_branch(self.case, POLICY, history, outcome)['published'])
            rendered, metadata = RollingExperience(path / 'experience' / POLICY).snapshot('c999')
            self.assertEqual(metadata['entries'], 1)
            # Check the stored row as well as the original helper's summary.
            committed = adapter.memories[POLICY].path.read_text()
            self.assertIn(p1.text, committed)
            for text in (rendered, committed):
                self.assert_black_box(text)


if __name__ == '__main__':
    unittest.main()
