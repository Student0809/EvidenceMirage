"""Recovery provenance/accounting tests using the real RecoveryPrefix validator.

Only corpus lookup and the imported initialization are small local fixtures;
source event validation, source hashing, episode reconstruction and policy replay
are real. Native scorer calls are mocked because these are synthetic rollouts.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.summarize_full_evidence import analyze, render_markdown
from src.engine.budget import Budget
from src.engine.history import History
from src.engine.prefix_recovery import RecoveryPrefix, ORIGIN
from src.engine.storage import plain, write_json
from src.evidence.ledger import EvidenceLedger
from src.evidence.stage import estimate_stage, response_signal
from src.policies.stage_aware import StageAware
from src.schema import Episode, Payload, TargetResult


def rows(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(plain(value))+'\n' for value in values))


class PrefixRecoverySummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.run = self.root/'source', self.root/'recovered'
        self.case = dict(case_id='c000', suite_name='fixture', user_task_id='user_task_0',
            injection_task_id='injection_task_0', user_task_prompt='Public task',
            injection_task_goal='Public goal', context_with_placeholder='{INJECTION:slot}',
            goal_spec=dict(trigger_ids=['observed'], predicates=[dict(id='observed',
                query='email_received', kind='event', public_goal_quote='Public goal')]))
        self.payload = Payload('P0', 'Synthetic imported candidate')
        self.initial = dict(payload=self.payload.text, analysis='Synthetic imported analysis',
                            strategy_ids=['fixture_strategy'], strategy_paths=['fixture_strategy.md'])
        self.config = dict(backend='agentdojo', split='benchmark', seed=42,
            post_failure_target_budget=4, policies=dict(stage_aware=dict(max_retries_per_payload=2)),
            attacker=dict(mode='piminer_aligned', initialization_manifest='fixture.json'))
        source_manifest = dict(config=self.config, cases=[self.case], case_ids=['c000'],
            environment={}, inputs_sha256={'src/attacker/experience.py': 'old',
                'data/cases.jsonl': 'case-data', 'resources/strategy.md': 'strategy-data'},
            limits=dict(preflight_model_calls=3))
        self.current = dict(deepcopy(source_manifest), inputs_sha256={
            'src/attacker/experience.py': 'fixed', 'src/engine/prefix_recovery.py': 'recovery',
            'data/cases.jsonl': 'case-data', 'resources/strategy.md': 'strategy-data'})
        lookup = patch('src.engine.prefix_recovery.load_cases', return_value=[self.case])
        bundle = patch('src.engine.prefix_recovery.InitializationBundle')
        lookup.start()
        self.addCleanup(lookup.stop)
        bundle.start().return_value.for_case.return_value = self.initial
        self.addCleanup(bundle.stop)
        write_json(self.source/'manifest.json', source_manifest)
        write_json(self.source/'status.json', dict(status='completed', selected_cases=1, completed_workers=1))
        write_json(self.source/'preflight.json', dict(native=dict(status='passed'),
            target=dict(status='passed', calls=[dict(status='completed')]),
            attacker=dict(status='passed', attacker=dict(calls=[dict(status='completed')]*2))))
        source_case = self.source/'c000'
        write_json(source_case/'manifest.json', dict(source_manifest, parent_run=str(self.source)))
        write_json(source_case/'status.json', dict(status='completed'))
        self.prefix = [self.episode(i, False, self.payload) for i in range(2)]
        for episode, raw in self.prefix:
            write_json(source_case/episode['raw_path'], raw)
        rows(source_case/'episodes.jsonl', [episode for episode, _ in self.prefix])
        decisions = self.decisions([episode for episode, _ in self.prefix])
        decisions.append(dict(decisions[-1], decision_id=decisions[-1]['decision_id']+'/error',
                              action='Stop', reason='GENERATION_ERROR'))
        rows(source_case/'decisions.jsonl', decisions)
        outcome = dict(case_id='c000', policy_id='stage_aware', initial_gold=False,
            gold_success=None, status='GENERATION_ERROR', target_attempts=1,
            shared_h1_id='c000/shared/000', revisions=1)
        write_json(source_case/'outcomes.json', [outcome])
        write_json(self.source/'worker_results.json', [dict(case_id='c000', status='completed',
                                                          outcomes=[outcome], target_executions=2)])
        p0 = dict(status='ready', mode='piminer_aligned', calls=[], candidates=[],
            payload=self.payload.text, payload_sha256=self.payload.sha256, analysis=self.initial['analysis'],
            generation_id='c000/shared/P0', reused_target_feedback=False,
            routing=dict(strategy_ids=self.initial['strategy_ids'], strategy_paths=self.initial['strategy_paths']))
        p1 = dict(status='GENERATION_ERROR', error_type='OSError', mode='piminer_aligned',
            calls=[], candidates=[], checks=[], generation_id='c000/stage_aware/P1',
            case_id='c000', policy_id='stage_aware')
        write_json(source_case/'raw/c000/shared/P0.generation.json', p0)
        write_json(source_case/'raw/c000/stage_aware/P1.generation.json', p1)
        events = [dict(event='GENERATION_STARTED', generation_id='c000/shared/P0'),
            dict(event='GENERATION_COMPLETED', generation_id='c000/shared/P0', status='ready',
                 raw_path='raw/c000/shared/P0.generation.json')]
        for episode, _ in self.prefix:
            events += self.target_events(episode)
        events += [dict(event='GENERATION_STARTED', generation_id='c000/stage_aware/P1'),
            dict(event='GENERATION_COMPLETED', generation_id='c000/stage_aware/P1', status='GENERATION_ERROR',
                 raw_path='raw/c000/stage_aware/P1.generation.json'),
            dict(event='EXPERIENCE_COMMITTED', case_id='c000', policy_id='stage_aware', published=False,
                 path=str(self.source/'experience/stage_aware/intra_dataset_experience.jsonl'))]
        rows(source_case/'events.jsonl', events)
        self.validated = RecoveryPrefix(self.source, self.current)
        self.current.update(recovery=deepcopy(self.validated.metadata), fresh_initial=False,
            reused_initial=True, reused_initial_scope='completed_prefix_of_same_experiment')
        self.make_destination()

    def episode(self, ordinal, gold, payload):
        policy = 'shared' if ordinal == 0 else 'stage_aware'
        eid = f'c000/{policy}/{ordinal:03d}'
        result = TargetResult(gold, final='PUBLIC_SYNTHETIC_FINAL', utility=True,
                              black_box_final='PUBLIC_SYNTHETIC_FINAL', seconds=1)
        ledger = EvidenceLedger(eid, self.case['goal_spec'])
        episode = Episode(eid, 'c000', policy, payload.id, payload.sha256,
            'initial' if ordinal == 0 else 'post_failure',
            'VALID_GOLD_TRUE' if gold else 'VALID_GOLD_FALSE', gold, result.final,
            ledger.trigger, estimate_stage(gold, ledger.trigger), response_signal(result.final),
            ledger.records(), None, None, None, None, 1, 0, 'raw/'+eid+'.json')
        return plain(episode), plain(dict(payload=payload.text, result=result, query_packets=[]))

    def decisions(self, episodes):
        decisions = []
        for ordinal, episode in enumerate(episodes):
            history = History.from_episodes([Episode(**item) for item in episodes[:ordinal+1]], Budget(4, ordinal))
            decision = StageAware().decide(history)
            decisions.append(dict(decision_id=f'c000/stage_aware/d{ordinal+1:03d}',
                case_id='c000', policy_id='stage_aware', visible_history_ids=[item['episode_id'] for item in episodes[:ordinal+1]],
                current_stage=episode['stage'], history=history.summary(), remaining_target_budget=4-ordinal,
                spent_target_budget=ordinal, action=decision.action.value, reason=decision.reason))
        return decisions

    def target_events(self, episode, recovered=False):
        provenance = dict(record_origin=ORIGIN, source_run=str(self.source),
                          source_episode_id=episode['episode_id']) if recovered else {}
        return [dict(event='TARGET_STARTED', episode_id=episode['episode_id'],
                     payload_sha256=episode['payload_sha256'], **provenance),
                dict(event='TARGET_COMPLETED', episode_id=episode['episode_id'], status=episode['status'], **provenance)]

    def make_destination(self):
        write_json(self.run/'manifest.json', self.current)
        write_json(self.run/'status.json', dict(status='completed'))
        directory = self.run/'c000'
        write_json(directory/'manifest.json', dict(self.current, parent_run=str(self.run)))
        write_json(directory/'status.json', dict(status='completed'))
        episodes, events = [], []
        for record in self.validated.for_case(self.case):
            episode = record['episode']
            copied = directory/episode['raw_path']
            copied.parent.mkdir(parents=True, exist_ok=True)
            copied.write_bytes((self.source/'c000'/record['source_raw_path']).read_bytes())
            episodes.append(episode)
            events += self.target_events(episode, recovered=True)
            events.append(dict(event='PREFIX_EXECUTION_RECOVERED', episode_id=episode['episode_id'],
                source_episode_id=record['source_episode_id'], source_run=str(self.source),
                record_origin=ORIGIN, source_raw_sha256=record['source_raw_sha256']))
        new, raw = self.episode(2, True, Payload('P1', 'Synthetic new candidate'))
        write_json(directory/new['raw_path'], raw)
        episodes.append(new)
        events += self.target_events(new)
        rows(directory/'episodes.jsonl', episodes)
        rows(directory/'events.jsonl', events)
        rows(directory/'decisions.jsonl', self.decisions(episodes))
        write_json(directory/'outcomes.json', [dict(case_id='c000', policy_id='stage_aware',
            gold_success=True, initial_gold=False, status='VALID_GOLD_TRUE', target_attempts=2)])

    def report(self, **kwargs):
        with patch('scripts.verify_observable_pilot5_20261029.verify_episode',
                   return_value=dict(status='native_scores_recomputed')) as native:
            report = analyze(self.run, verify_completed=True, **kwargs)
        return report, native.call_count

    def test_real_source_validator_and_copy_checks_count_source_plus_new_once(self):
        report, native_calls = self.report()
        self.assertEqual(report['verification']['status'], 'passed')
        self.assertEqual(native_calls, 3)
        self.assertEqual(report['verification']['decisions_replayed'], 3)
        self.assertEqual(report['target_started'], 3)
        self.assertEqual(report['target_completed'], 3)
        recovery = report['recovery']
        self.assertEqual(recovery['source_validation'], 'passed')
        self.assertEqual(recovery['status'], 'passed')
        self.assertEqual(recovery['source_prefix_target_executions'], 2)
        self.assertEqual(recovery['carried_target_completed'], 2)
        self.assertEqual(recovery['new_target_completed'], 1)
        self.assertEqual(recovery['total_unique_target_completed'], 3)
        self.assertEqual(recovery['verified_carried_episodes'], 2)
        self.assertEqual(recovery['source_preflight']['model_calls'], 3)
        self.assertTrue(recovery['source_preflight']['reused'])
        self.assertIn('不重发请求', render_markdown(report))
        self.assertEqual([row['record_origin'] for row in report['cases'][0]['timeline']],
                         [ORIGIN, ORIGIN, 'new_execution'])

    def test_legacy_code_audit_metadata_is_optional_and_code_entries_are_ignored(self):
        manifest = deepcopy(self.current)
        manifest['inputs_sha256']['src/attacker/experience.py'] = 'any-new-source'
        manifest['inputs_sha256']['new/implementation.py'] = 'added-source'
        manifest['inputs_sha256'].pop('src/engine/prefix_recovery.py')
        manifest['recovery'].update(changed_input_paths=['src/arbitrary.py'],
                                    added_input_paths=['new/implementation.py'])
        write_json(self.run/'manifest.json', manifest)
        # The destination child retains its old Python entries; only data and
        # resources must match the parent, preserving old-manifest compatibility.
        report, _ = self.report()
        self.assertEqual(report['recovery']['source_validation'], 'passed')
        self.assertEqual(report['verification']['status'], 'passed')
        self.assertEqual(report['target_completed'], 3)

    def test_source_parent_child_python_entries_may_differ_but_data_may_not(self):
        path = self.source/'c000/manifest.json'
        child = json.loads(path.read_text())
        child['inputs_sha256'].pop('src/attacker/experience.py')
        child['inputs_sha256']['other/child_source.py'] = 'new-source'
        write_json(path, child)
        recovery = RecoveryPrefix(self.source, self.current)
        self.assertEqual(recovery.metadata['prefix_target_executions'], 2)
        child['inputs_sha256']['data/cases.jsonl'] = 'changed-data'
        write_json(path, child)
        with self.assertRaisesRegex(ValueError, 'child manifest differs'):
            RecoveryPrefix(self.source, self.current)

    def test_changed_missing_and_added_data_or_resources_still_block_recovery(self):
        for kind in ('changed', 'missing', 'added', 'resource'):
            manifest = deepcopy(self.current)
            if kind == 'changed':
                manifest['inputs_sha256']['data/cases.jsonl'] = 'changed-data'
            elif kind == 'missing':
                manifest['inputs_sha256'].pop('data/cases.jsonl')
            elif kind == 'resource':
                manifest['inputs_sha256']['resources/strategy.md'] = 'changed-resource'
            else:
                manifest['inputs_sha256']['data/new.json'] = 'extra-data'
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'data/resource fingerprints differ'):
                RecoveryPrefix(self.source, manifest)

    def test_source_raw_tamper_invalidates_declared_snapshot(self):
        source_raw = self.source/'c000'/self.prefix[0][0]['raw_path']
        source_raw.write_bytes(source_raw.read_bytes()+b' ')
        report, _ = self.report()
        self.assertEqual(report['recovery']['source_validation'], 'failed')
        self.assertEqual(report['verification']['status'], 'failed')
        self.assertIn('recovery_source_validation_failed', [issue['code'] for issue in report['issues']])

    def test_copied_raw_must_match_source_bytes_not_only_json_values(self):
        copied = self.run/'c000'/self.prefix[0][0]['raw_path']
        copied.write_text(json.dumps(json.loads(copied.read_text())))
        report, _ = self.report()
        self.assertEqual(report['recovery']['status'], 'failed')
        self.assertIn('recovered_raw_bytes_mismatch', [issue['code'] for issue in report['issues']])

    def test_episode_fields_cannot_be_rewritten_in_recovered_prefix(self):
        path = self.run/'c000/episodes.jsonl'
        episodes = [json.loads(line) for line in path.read_text().splitlines()]
        episodes[0]['target_seconds'] = 500
        rows(path, episodes)
        report, _ = self.report()
        self.assertIn('recovered_episode_mismatch', [issue['code'] for issue in report['issues']])
        self.assertEqual(report['verification']['status'], 'failed')

    def test_copied_records_without_explicit_recovery_manifest_are_rejected(self):
        manifest = deepcopy(self.current)
        manifest.pop('recovery')
        write_json(self.run/'manifest.json', manifest)
        report, _ = self.report()
        codes = [issue['code'] for issue in report['issues']]
        self.assertIn('unauthorized_recovered_execution', codes)
        self.assertIn('recovery_manifest_missing', codes)
        self.assertEqual(report['verification']['status'], 'failed')

    def test_declared_recovery_does_not_authorize_missing_per_episode_provenance(self):
        path = self.run/'c000/events.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events = [event for event in events if event['event'] != 'PREFIX_EXECUTION_RECOVERED']
        for event in events:
            for key in ('record_origin', 'source_run', 'source_episode_id'):
                event.pop(key, None)
        rows(path, events)
        report, _ = self.report()
        self.assertEqual(report['recovery']['status'], 'failed')
        self.assertIn('recovered_episode_without_provenance', [issue['code'] for issue in report['issues']])

    def test_tampered_recovery_count_is_not_accepted_from_manifest(self):
        manifest = deepcopy(self.current)
        manifest['recovery']['prefix_target_executions'] = 100
        write_json(self.run/'manifest.json', manifest)
        report, _ = self.report()
        self.assertEqual(report['recovery']['source_validation'], 'failed')
        self.assertEqual(report['verification']['status'], 'failed')

    def test_running_worker_can_await_copy_without_losing_known_source_execution_count(self):
        directory = self.run/'c000'
        for name in ('episodes', 'decisions', 'events'):
            rows(directory/(name+'.jsonl'), [])
        (directory/'outcomes.json').unlink()
        write_json(directory/'status.json', dict(status='running'))
        report, native_calls = self.report()
        self.assertEqual(native_calls, 0)
        self.assertEqual(report['recovery']['status'], 'passed')
        self.assertEqual(report['recovery']['carried_target_completed'], 0)
        self.assertEqual(report['recovery']['new_target_completed'], 0)
        self.assertEqual(report['recovery']['total_unique_target_completed'], 2)
        self.assertIsNone(report['asr']['complete_asr'])

    def test_live_partial_prefix_write_is_partial_then_strict_after_completion(self):
        directory = self.run/'c000'
        first = self.prefix[0][0]
        rows(directory/'episodes.jsonl', [first])
        rows(directory/'events.jsonl', self.target_events(first, recovered=True)[:1])
        rows(directory/'decisions.jsonl', [])
        (directory/'outcomes.json').unlink()
        write_json(directory/'status.json', dict(status='running'))
        report, calls = self.report()
        self.assertEqual(calls, 0)
        self.assertEqual(report['recovery']['status'], 'partial')
        self.assertGreater(report['recovery']['pending_record_count'], 0)
        self.assertEqual(report['recovery']['verified_carried_episodes'], 0)
        self.assertEqual(report['recovery']['total_unique_target_completed'], 2)
        write_json(directory/'status.json', dict(status='completed'))
        report, _ = self.report()
        self.assertEqual(report['recovery']['status'], 'failed')
        self.assertEqual(report['verification']['status'], 'failed')

    def test_ordinary_run_remains_compatible_and_reused_event_still_fails(self):
        manifest = deepcopy(self.current)
        for key in ('recovery', 'fresh_initial', 'reused_initial', 'reused_initial_scope'):
            manifest.pop(key, None)
        write_json(self.run/'manifest.json', manifest)
        write_json(self.run/'c000/manifest.json', dict(manifest, parent_run=str(self.run)))
        path = self.run/'c000/events.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events = [event for event in events if event['event'] != 'PREFIX_EXECUTION_RECOVERED']
        for event in events:
            for key in ('record_origin', 'source_run', 'source_episode_id'):
                event.pop(key, None)
        rows(path, events)
        report, native_calls = self.report()
        self.assertEqual(native_calls, 3)
        self.assertEqual(report['verification']['status'], 'passed')
        self.assertEqual(report['recovery']['status'], 'not_applicable')
        self.assertEqual(report['recovery']['new_target_completed'], 3)
        self.assertEqual(report['recovery']['carried_target_completed'], 0)
        events.append(dict(event='TARGET_REUSED', episode_id='c000/shared/000'))
        rows(path, events)
        report, _ = self.report()
        self.assertEqual(report['verification']['status'], 'failed')
        self.assertIn('reused_execution', [issue['code'] for issue in report['cases'][0]['verification']['issues']])


if __name__ == '__main__':
    unittest.main()
