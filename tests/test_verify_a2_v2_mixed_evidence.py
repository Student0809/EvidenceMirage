"""Offline audit witnesses: reject changed prompts/provenance and preserve errors."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify_a2_v2_test30_20261030 import (
    black_box_prompt, experience_protocol, false_packets,
    public_final, terminal_target_error, verify_decision_routes, verify_grounded,
    Reconstruction, revision_observation, frozen_history_v1,
)
from src.attacker.piminer import GenerationError, PUBLIC_FIELDS
from src.engine.config import ROOT
from src.evidence.ledger import EvidenceLedger
import tests.test_aligned_evidence_false as fixture
from tests.test_plan_continuity import ContinuityDecision


class MixedAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AlignedEvidenceFalseTests.setUpClass()
        cls.fixture = fixture.AlignedEvidenceFalseTests(
            'test_false_uses_real_planner_checker_a2_document_and_projected_final')
        cls.episode = json.loads((ROOT / fixture.REPLAY).parents[3]
            .joinpath('episodes.jsonl').read_text().splitlines()[0])
        original_failure=cls.fixture.failure
        cls.fixture.failure=lambda index,trigger='F',**updates:original_failure(
            index,trigger,policy_id='shared' if index==0 else 'stage_aware',**updates)
        cls.config = dict(attacker=dict(model='offline', effort='max'),
                          policies=dict(stage_aware=dict(unknown_action='revise')))

    def stored(self, record, history, p0):
        """Fill transport metadata in fake outputs; never create a transport."""
        record = deepcopy(record)
        record['generation_id'] = 'c004/stage_aware/P1'
        for call in record['calls']:
            call['provider'].update(model='offline', effort='max')
        episode = deepcopy(self.episode)
        episode.update(final='PRIVATE_EPISODE_FINAL', payload_sha256=p0.sha256)
        saved = dict(payload=p0.text, query_packets=self.fixture.packets(self.fixture.failure(0)))
        return record, episode, saved

    def test_grounded_replay_reconstructs_all_stages_without_model_transport(self):
        # A malformed first candidate exercises the optional repair prompt too.
        decision = fixture.EvidenceDecision(self.fixture.case['injection_task_goal'],
            candidates=['NO_INJECTION_BLOCK', '<injection>REPAIRED_PUBLIC_CANDIDATE</injection>'])
        with self.fixture.branch(decide=decision) as (adapter, _, _, p0, history, _):
            _, generated = self.fixture.revise(adapter, p0, history, 1)
            record, episode, saved = self.stored(generated, history, p0)
            with patch('src.attacker.piminer.ClaudeDecision', side_effect=AssertionError('No model client')):
                result = verify_grounded(record, self.fixture.case, episode, saved, history,
                    adapter.routes['c004']['document'], self.config)
            self.assertEqual(result['saved_outputs_replayed'], 4)
            self.assertEqual(result['api_calls'], 0)
            self.assertEqual(len(record['checks']), 2)

    def test_grounded_replay_rejects_prompt_plan_candidate_check_and_analysis_changes(self):
        with self.fixture.branch() as (adapter, _, _, p0, history, _):
            _, generated = self.fixture.revise(adapter, p0, history, 1)
            original, episode, saved = self.stored(generated, history, p0)
            changes = (
                lambda r: r['calls'][0].update(prompt=r['calls'][0]['prompt'] + 'PRIVATE_STATE'),
                lambda r: r['calls'][1].update(prompt=r['calls'][1]['prompt'] + 'EXTRA_EVIDENCE'),
                lambda r: r['plan'].update(hypothesis='Changed saved plan'),
                lambda r: r['candidates'].append('<injection>EXTRA</injection>'),
                lambda r: r['checks'][0].update(status='TAMPERED'),
                lambda r: r['revision_memory']['candidates'][0].update(text='TAMPERED_ARCHIVE'),
                lambda r: r['revision_memory']['facts'].append(dict(id='FORGED',kind='public_input',value='x')),
                lambda r: r['planning']['actions'].append(dict(error='TAMPERED_TRACE')),
                lambda r: r['semantic_checks'][0]['assessments'].update(plan_following='consistent'),
                lambda r: r.update(parent_payload_sha256='TAMPERED_PARENT'),
                lambda r: r.update(analysis=fixture.ANALYSIS_ONLY),
                lambda r: r.update(session_id='U_SESSION'),
            )
            for change in changes:
                with self.subTest(change=change):
                    record = deepcopy(original)
                    change(record)
                    with self.assertRaises(ValueError):
                        verify_grounded(record, self.fixture.case, episode, saved, history,
                            adapter.routes['c004']['document'], self.config)

    def test_grounded_replay_rejects_wrong_latest_final_document_or_packet_source(self):
        with self.fixture.branch() as (adapter, _, _, p0, history, _):
            _, generated = self.fixture.revise(adapter, p0, history, 1)
            record, episode, saved = self.stored(generated, history, p0)
            wrong_history = deepcopy(history)
            wrong_history[-1]['final_output'] = 'OTHER_ATTEMPT_FEEDBACK'
            with self.assertRaises(ValueError):
                verify_grounded(record, self.fixture.case, episode, saved, wrong_history,
                    adapter.routes['c004']['document'], self.config)
            with self.assertRaises(ValueError):
                verify_grounded(record, self.fixture.case, episode, saved, history,
                    'OTHER_A2_DOCUMENT', self.config)
            saved['query_packets'][0]['episode_id'] = 'c999/shared/000'
            with self.assertRaises(ValueError):
                verify_grounded(record, self.fixture.case, episode, saved, history,
                    adapter.routes['c004']['document'], self.config)

    def test_new_replay_uses_independent_earlier_history_observations(self):
        decision = ContinuityDecision(self.fixture.case['injection_task_goal'])
        with self.fixture.branch(decide=decision) as (adapter,_,_,p0,history,_):
            p1,first=self.fixture.revise(adapter,p0,history,1)
            initial_episode=deepcopy(self.episode)
            initial_episode['payload_sha256']=p0.sha256
            initial_saved=dict(payload=p0.text,query_packets=self.fixture.packets(self.fixture.failure(0)))
            history[0]['revision_observation']=revision_observation(
                self.fixture.case,initial_episode,initial_saved)
            history.append(self.fixture.attempt(p1,first['analysis'],1))
            history[-1]['revision_plan'] = deepcopy(first['revision_plan'])
            _,generated=self.fixture.revise(adapter,p1,history,2)
            record=deepcopy(generated)
            record['generation_id']='c004/stage_aware/P2'
            for call in record['calls']:
                call['provider'].update(model='offline',effort='max')
            episode=deepcopy(initial_episode)
            episode.update(episode_id='c004/stage_aware/001',policy_id='stage_aware',
                           payload_id='P1',payload_sha256=p1.sha256)
            saved=dict(payload=p1.text,query_packets=self.fixture.packets(self.fixture.failure(1)))
            ledger=EvidenceLedger(episode['episode_id'],self.fixture.case['goal_spec'])
            for packet in saved['query_packets']:ledger.update(packet)
            episode['evidence']=list(ledger.records())
            replay=verify_grounded(record,self.fixture.case,episode,saved,history,
                adapter.routes['c004']['document'],self.config)
            self.assertEqual(replay['saved_outputs_replayed'],3)
            self.assertEqual(record['revision_memory']['attempts'][1]['revision_plan'],
                             first['revision_plan'])
            wrong_plan = deepcopy(history)
            wrong_plan[-1]['revision_plan']['summary']['hypothesis'] = 'ALTERED_PRIOR_PLAN'
            with self.assertRaises(ValueError):
                verify_grounded(record, self.fixture.case, episode, saved, wrong_plan,
                    adapter.routes['c004']['document'], self.config)
            incomplete=deepcopy(history)
            del incomplete[0]['revision_observation']
            with self.assertRaises(ValueError):
                verify_grounded(record,self.fixture.case,episode,saved,incomplete,
                    adapter.routes['c004']['document'],self.config)

    def test_grounded_saved_model_failure_is_replayed_and_retained(self):
        for stage in ('planner', 'generator'):
            decision = fixture.EvidenceDecision(self.fixture.case['injection_task_goal'], fail_stage=stage)
            with self.subTest(stage=stage), self.fixture.branch(decide=decision) as (adapter, _, _, p0, history, _):
                with self.assertRaises(GenerationError) as caught:
                    self.fixture.revise(adapter, p0, history, 1)
                record, episode, saved = self.stored(caught.exception.record, history, p0)
                replay = verify_grounded(record, self.fixture.case, episode, saved, history,
                    adapter.routes['c004']['document'], self.config)
                self.assertEqual(replay['saved_outputs_replayed'], 1 if stage == 'planner' else 2)
                self.assertEqual(record['status'], 'GENERATION_ERROR')

    def test_completed_planner_with_invalid_action_stops_without_generator(self):
        class QueryDecision(fixture.EvidenceDecision):
            def __call__(self, prompt, stage):
                output = super().__call__(prompt, stage)
                return (json.dumps(dict(action=self.action, condition_id='payment_received',
                                        reason='Request another diagnostic'))
                        if stage == 'planner' else output)

        for action in (None, 'query_subgoal'):
            decision = QueryDecision(self.fixture.case['injection_task_goal'])
            decision.action = action
            with self.subTest(action=action), self.fixture.branch(decide=decision) as (adapter, _, _, p0, history, _):
                with self.assertRaises(GenerationError) as caught:
                    self.fixture.revise(adapter, p0, history, 1)
                record, episode, saved = self.stored(caught.exception.record, history, p0)
                replay = verify_grounded(record, self.fixture.case, episode, saved, history,
                    adapter.routes['c004']['document'], self.config)
                self.assertEqual(len(record['calls']), 2)
                self.assertEqual(record['calls'][0]['status'], 'completed')
                self.assertEqual(record['candidates'], [])
                self.assertEqual(record['checks'], [])
                self.assertTrue(any('queries unavailable' in error['reason'] for error in replay['error_chain']))

    def test_grounded_transport_value_error_is_replayed_without_correction_or_error_text(self):
        class TransportDecision(fixture.EvidenceDecision):
            def __call__(self,prompt,stage):
                if stage=='planner':
                    self.calls.append(dict(stage=stage,prompt=prompt,status='error',
                                           error_type='ValueError',seconds=0))
                    raise ValueError('UNSAVED_TRANSPORT_DETAIL')
                return super().__call__(prompt,stage)
        decision=TransportDecision(self.fixture.case['injection_task_goal'])
        with self.fixture.branch(decide=decision) as (adapter,_,_,p0,history,_):
            with self.assertRaises(GenerationError) as caught:
                self.fixture.revise(adapter,p0,history,1)
            record,episode,saved=self.stored(caught.exception.record,history,p0)
            replay=verify_grounded(record,self.fixture.case,episode,saved,history,
                adapter.routes['c004']['document'],self.config)
            self.assertEqual(replay['saved_outputs_replayed'],1)
            self.assertEqual(record['calls'][0]['error_type'],'ValueError')
            self.assertNotIn('UNSAVED_TRANSPORT_DETAIL',json.dumps(record))

    def test_actual_saved_legacy_success_and_malformed_planner_remain_replayable(self):
        run=ROOT/'runs/a2_v2_mixed_evidence_test30_20261030_r2'
        manifest=json.loads((run/'manifest.json').read_text())
        reconstruction=Reconstruction(manifest,run)
        for cid,revision,expected_calls in (('c004','P1',2),('c018','P2',1)):
            with self.subTest(case_id=cid):
                case=next(c for c in manifest['cases'] if c['case_id']==cid)
                directory=run/cid
                record=json.loads((directory/f'raw/{cid}/stage_aware/{revision}.generation.json').read_text())
                episodes=[json.loads(line) for line in (directory/'episodes.jsonl').read_text().splitlines()]
                history=[]
                for episode in episodes:
                    saved=json.loads((directory/episode['raw_path']).read_text())
                    branch='shared' if episode['payload_id']=='P0' else 'stage_aware'
                    generation=json.loads((directory/f"raw/{cid}/{branch}/{episode['payload_id']}.generation.json").read_text())
                    history.append(dict(iter=len(history),analysis=generation['analysis'],
                        injection=saved['payload'],security=saved['result']['gold'],
                        utility=saved['result']['utility'],final_output=public_final(saved['result'])))
                    if episode['episode_id']==record['evidence_episode_id']:
                        break
                document=reconstruction.compose(case)[1]
                with patch('src.attacker.piminer.ClaudeDecision',side_effect=AssertionError('No model client')):
                    replay=verify_grounded(record,case,episode,saved,history,document,manifest['config'])
                self.assertEqual(record['prompt_mode'],'grounded_latest')
                self.assertEqual(replay['saved_outputs_replayed'],expected_calls)
                self.assertEqual(replay['api_calls'],0)
                changed=deepcopy(record)
                changed['calls'][0]['prompt']+='TAMPERED_LEGACY_PROMPT'
                with self.assertRaises(ValueError):
                    verify_grounded(changed,case,episode,saved,history,document,manifest['config'])

    def test_actual_saved_history_v1_success_and_schema_failure_remain_replayable(self):
        run = ROOT/'runs/a2_v2_terminal_history_test30_20261031_172750'
        manifest = json.loads((run/'manifest.json').read_text())
        reconstruction = Reconstruction(manifest, run)
        historical = frozen_history_v1()
        for cid, expected_calls in (('c004', 3), ('c018', 2)):
            with self.subTest(case_id=cid):
                case = next(c for c in manifest['cases'] if c['case_id'] == cid)
                directory = run/cid
                record = json.loads((directory/f'raw/{cid}/stage_aware/P1.generation.json').read_text())
                episodes = [json.loads(line) for line in (directory/'episodes.jsonl').read_text().splitlines()]
                history = []
                for episode in episodes:
                    saved = json.loads((directory/episode['raw_path']).read_text())
                    branch = 'shared' if episode['payload_id'] == 'P0' else 'stage_aware'
                    generation = json.loads((directory/f"raw/{cid}/{branch}/{episode['payload_id']}.generation.json").read_text())
                    history.append(dict(iter=len(history), analysis=generation['analysis'],
                        injection=saved['payload'], security=saved['result']['gold'],
                        utility=saved['result']['utility'], final_output=public_final(saved['result']),
                        revision_observation=revision_observation(case, episode, saved)))
                    if episode['episode_id'] == record['evidence_episode_id']:
                        break
                document = reconstruction.compose(case)[1]
                with patch.object(historical, 'ClaudeDecision', side_effect=AssertionError('No model client')):
                    replay = verify_grounded(record, case, episode, saved, history, document, manifest['config'])
                self.assertEqual(replay['saved_outputs_replayed'], expected_calls)
                self.assertEqual(replay['api_calls'], 0)
                if cid == 'c018':
                    self.assertIn('Invalid edit or reference to unavailable evidence',
                                  [error['reason'] for error in replay['error_chain']])
                changed = deepcopy(record)
                changed['calls'][0]['prompt'] += 'TAMPERED_V1_PROMPT'
                with self.assertRaises(ValueError):
                    verify_grounded(changed, case, episode, saved, history, document, manifest['config'])

    def test_plan_v2_replay_preserves_bounded_replanning_and_rejects_tampering(self):
        class BoundedReplanDecision(ContinuityDecision):
            def __init__(self, *args):
                super().__init__(*args)
                self.planner_requests = self.revision_requests = 0

            def __call__(self, prompt, stage):
                result = super().__call__(prompt, 'planner' if stage == 'plan_revision' else stage)
                self.calls[-1]['stage'] = stage
                if stage == 'planner':
                    self.planner_requests += 1
                    if self.planner_requests == 1:
                        return 'not JSON'
                if stage == 'plan_revision':
                    self.revision_requests += 1
                    if self.revision_requests == 1:
                        return 'not JSON'
                    plan = json.loads(result)
                    plan['hypothesis'] = 'CORRECTED_PLAN'
                    return json.dumps(plan)
                if stage == 'semantic_review' and self.generated == 1:
                    result['assessments']['plan_goal_alignment'] = 'conflict'
                    result['findings'] = [dict(dimension='plan_goal_alignment',
                        explanation='Fixture: reconsider the plan.',
                        quotes=[dict(source='plan', text=fixture.PLAN_ONLY)])]
                return result

        decision = BoundedReplanDecision(self.fixture.case['injection_task_goal'])
        with self.fixture.branch(decide=decision) as (adapter, _, _, p0, history, _):
            _, generated = self.fixture.revise(adapter, p0, history, 1)
            record, episode, saved = self.stored(generated, history, p0)
            document = adapter.routes['c004']['document']
            self.assertEqual(record['prompt_mode'], 'grounded_history_plan_v2')
            self.assertEqual([call['stage'] for call in record['calls']],
                ['planner', 'planner', 'generator', 'semantic_review',
                 'plan_revision', 'plan_revision', 'static_repair', 'semantic_review'])
            with patch('src.attacker.piminer.ClaudeDecision', side_effect=AssertionError('No model client')):
                replay = verify_grounded(record, self.fixture.case, episode, saved, history, document, self.config)
            self.assertEqual(replay['saved_outputs_replayed'], 8)
            self.assertEqual(record['revision_plan']['plan_id'], 'c004/stage_aware/P1')
            for change in (
                    lambda r: r['calls'][4].update(prompt=r['calls'][4]['prompt'] + 'TAMPERED_REPLAN'),
                    lambda r: r['plan_revisions'][0]['revised_plan'].update(hypothesis='ALTERED_PLAN'),
                    lambda r: r['revision_plan'].update(policy_id='shared'),
                    lambda r: r.update(policy_id='foreign'),
                    lambda r: r['calls'].append(deepcopy(r['calls'][-1]))):
                changed = deepcopy(record)
                change(changed)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    verify_grounded(changed, self.fixture.case, episode, saved, history, document, self.config)

    def test_private_packet_metadata_is_excluded_but_source_forgery_is_rejected(self):
        episode = deepcopy(self.episode)
        packets = self.fixture.packets(self.fixture.failure(0))
        packets[0]['private_state'] = 'PRIVATE_EVALUATOR_STATE'
        packets[0]['response']['private_state'] = 'PRIVATE_EVALUATOR_STATE'
        projected = false_packets(self.fixture.case, episode, packets)
        self.assertEqual(set(projected[0]), {'query_id', 'response'})
        self.assertEqual(set(projected[0]['response']), {'supported', 'evidence'})
        self.assertNotIn('PRIVATE_EVALUATOR_STATE', json.dumps(projected))
        packets[0]['source'] = 'private_evaluator_state'
        with self.assertRaises(ValueError):
            false_packets(self.fixture.case, episode, packets)

    def test_u_delta_across_f_has_every_unseen_public_attempt_without_f_analysis(self):
        public = {k: self.fixture.case[k] for k in PUBLIC_FIELDS}
        history = [dict(iter=i, analysis='' if i == 2 else f'PUBLIC_ANALYSIS_{i}',
                    injection=f'PUBLIC_INJECTION_{i}', security=False, utility=True,
                    final_output=f'PUBLIC_FINAL_{i}') for i in range(3)]
        prompt, mode = black_box_prompt('A2_STRATEGY', public, history, history[:1], '')
        self.assertEqual(mode, 'delta')
        self.assertNotIn('PUBLIC_FINAL_0', prompt)
        for i in (1, 2):
            self.assertIn(f'PUBLIC_FINAL_{i}', prompt)
            self.assertIn(f'PUBLIC_INJECTION_{i}', prompt)
        for marker in (fixture.PLAN_ONLY, fixture.EVIDENCE_ONLY, fixture.ANALYSIS_ONLY):
            self.assertNotIn(marker, prompt)

    def test_retry_requires_true_and_unknown_requires_revise(self):
        episodes = {'e': dict(gold=False, error=None, trigger='U')}
        decision = dict(visible_history_ids=['e'], action='Retry', reason='unknown_schedule')
        with self.assertRaisesRegex(ValueError, 'retry_without_observed_true'):
            verify_decision_routes([decision], episodes, self.config)
        decision.update(action='Revise', reason='unknown_piminer_revise')
        self.assertEqual(verify_decision_routes([decision], episodes, self.config), {'U_Revise': 1})
        episodes['e']['trigger'] = 'T'
        decision.update(action='Retry', reason='preserve_observed_trigger')
        self.assertEqual(verify_decision_routes([decision], episodes, self.config), {'T_Retry': 1})
        episodes['e']['trigger'] = 'F'
        with self.assertRaisesRegex(ValueError, 'retry_without_observed_true'):
            verify_decision_routes([decision], episodes, self.config)
        episodes['e']['trigger'] = 'U'
        decision.update(action='Stop', reason='budget_exhausted')
        self.assertEqual(verify_decision_routes([decision], episodes, self.config), {'U_Stop': 1})

    def test_black_box_projection_is_checked_against_raw_last_assistant_only(self):
        result = dict(final='PUBLIC_FINAL', black_box_final='PUBLIC_FINAL', raw=dict(messages=[
            dict(role='assistant', content='PRIVATE_INTERMEDIATE'),
            dict(role='tool', content='PRIVATE_TOOL_RESULT'),
            dict(role='assistant', content='PUBLIC_FINAL')]))
        self.assertEqual(public_final(result), 'PUBLIC_FINAL')
        result['black_box_final'] += '\nPRIVATE_TOOL_RESULT'
        with self.assertRaisesRegex(ValueError, 'black_box_final_not_last_assistant_projection'):
            public_final(result)

    def test_experience_disclosure_follows_actual_amendment(self):
        current = dict(experiment=dict(rolling_experience='Completed current-run cases share policy-scoped rolling experience'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            protocol, deviations = experience_protocol(path, current, 7)
            self.assertFalse(deviations)
            self.assertFalse(protocol['description_corrected'])
            amendment = dict(kind='descriptive_protocol_correction', runtime_changed=False)
            (path/'protocol_amendment_experience_20261030.json').write_text(json.dumps(amendment))
            old = dict(experiment=dict(rolling_experience='Fresh isolated per-case worker; no cross-case experience sharing'))
            protocol, deviations = experience_protocol(path, old, 9)
            self.assertTrue(protocol['description_corrected'])
            self.assertEqual(len(deviations), 1)
            self.assertEqual(deviations[0]['observed_nonempty_snapshots'], 9)
            self.assertEqual(deviations[0]['amendments'][0]['record'], amendment)

    def test_terminal_target_error_keeps_null_and_uses_only_terminal_provider_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            episode = dict(case_id='c000', episode_id='c000/stage_aware/004',
                           error='InternalServerError', gold=None, raw_path='error.json')
            result = dict(gold=None, raw=dict(error_details=dict(status_code=503,
                provider_code='system_memory_overloaded', provider_type='new_api_error',
                message='DO_NOT_PRINT_PROVIDER_MESSAGE'), api_calls=[dict(status='error',
                error_details=dict(status_code=504, provider_code='OTHER_RETRY_FAILURE'))]))
            (path/'error.json').write_text(json.dumps(dict(result=result)))
            (path/'outcomes.json').write_text(json.dumps([dict(status='TARGET_ERROR', gold_success=None)]))
            decisions = [dict(action='Stop', reason='target_error_not_gold_false')]
            summary = terminal_target_error(path, [episode], decisions, 'completed')
            self.assertIsNone(summary['official_gold'])
            self.assertIsNone(summary['outcome_gold_success'])
            self.assertEqual(summary['persisted_status_code'], 503)
            self.assertEqual(summary['persisted_provider_code'], 'system_memory_overloaded')
            self.assertNotIn('OTHER_RETRY_FAILURE', json.dumps(summary))
            self.assertNotIn('DO_NOT_PRINT_PROVIDER_MESSAGE', json.dumps(summary))
            episode['gold'] = False
            with self.assertRaisesRegex(ValueError, 'target_error_not_preserved_null'):
                terminal_target_error(path, [episode], decisions, 'completed')


if __name__ == '__main__':
    unittest.main()
