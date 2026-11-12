"""Offline fixtures for independent-repeat interpretation, errors, and completion costs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.summarize_evidence_repeat_20261030 import (
    EXPECTED_IDS, ROOT, analyze, main, render_markdown,
)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def write_jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(v)+'\n' for v in values))


class EvidenceRepeatSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run, self.reference = self.root/'repeat', self.root/'reference'
        self.secret = 'NEVER_OUTPUT_FIXTURE_PAYLOAD_TEXT'
        cases = [dict(case_id=cid, base_task_id='fixture/'+cid, p0=self.secret) for cid in EXPECTED_IDS]
        config = dict(backend='fixture', target=dict(model='target'), attacker=dict(model='generator'),
                      observation=dict(queries=['fixture']), require_observable_trigger=True, seed=20261029,
                      policies=dict(stage_aware=dict(max_retries_per_payload=2, unknown_schedule=['Retry', 'Revise'])))
        self.manifests = {}
        for root, budget in ((self.run, 2), (self.reference, 3)):
            cfg = deepcopy(config)
            cfg['post_failure_target_budget'] = budget
            if root == self.reference:
                cfg['policies']['stage_aware_no_evidence'] = deepcopy(cfg['policies']['stage_aware'])
            manifest = dict(cases=cases, config=cfg, environment=dict(python='fixture'),
                            inputs_sha256={'src/schema.py': 'legacy-source-hash'})
            self.manifests[root] = manifest
            write_json(root/'manifest.json', manifest)

    def add_case(self, root, cid, golds=(False, False, False), *, triggers=None,
                 actions=None, generation_error_at=None):
        folder = root/cid
        manifest = deepcopy(self.manifests[root])
        manifest['cases'] = [c for c in manifest['cases'] if c['case_id'] == cid]
        manifest['reused_initial'] = False
        write_json(folder/'manifest.json', manifest)
        write_json(folder/'status.json', dict(status='completed'))
        events, episodes, decisions, history = [], [], [], []
        triggers = triggers or ['F']*len(golds)
        actions = actions or ['Revise']*max(0, len(golds)-1)

        def generate(pid, number, error=False):
            gid = f'{cid}/{pid}/P{number}'
            raw_path = f'raw/{gid}.generation.json'
            events.extend([dict(event='GENERATION_STARTED', generation_id=gid),
                           dict(event='GENERATION_COMPLETED', generation_id=gid, raw_path=raw_path,
                                status='GENERATION_ERROR' if error else 'ready')])
            write_json(folder/raw_path, dict(generation_id=gid, policy_id=pid,
                status='GENERATION_ERROR' if error else 'ready', error_type='FixtureGenerationError' if error else None,
                analysis=self.secret, calls=[dict(stage='fixture', provider=dict(usage=dict(input_tokens=5, output_tokens=1)))]))

        def decide(number, action, reason='fixture'):
            if reason == 'fixture':
                reason = 'trigger_false' if episodes[-1]['trigger'] == 'F' else 'preserve_observed_trigger'
            decisions.append(dict(case_id=cid, policy_id='stage_aware',
                decision_id=f'{cid}/stage_aware/d{number:03d}', action=action, reason=reason,
                spent_target_budget=number-1, remaining_target_budget=manifest['config']['post_failure_target_budget']-number+1,
                visible_history_ids=list(history)))

        generate('shared', 0, error=generation_error_at == 0)
        for index, gold in enumerate(golds):
            pid, number = ('shared', 0) if index == 0 else ('stage_aware', index)
            if index:
                action = actions[index-1]
                decide(index, action)
                if action == 'Revise':
                    generate(pid, index)
            eid = f'{cid}/{pid}/{number:03d}'
            raw_path = f'raw/{eid}.json'
            episode = dict(case_id=cid, policy_id=pid, episode_id=eid, payload_id=f'P{index}',
                payload_sha256=hashlib.sha256(f'{root.name}/{cid}/{index}'.encode()).hexdigest(),
                phase='initial' if index == 0 else 'post_failure', gold=gold, trigger=triggers[index],
                final='', evidence=[], observation=None, observation_error=None, response_signal='fixture',
                stage='SUCCESS' if gold is True else 'ERROR' if gold is None else 'TRIGGERED_FAIL' if triggers[index] == 'T' else 'NOT_TRIGGERED',
                status='TARGET_ERROR' if gold is None else 'VALID_GOLD_TRUE' if gold else 'VALID_GOLD_FALSE',
                error='FixtureTargetError' if gold is None else None,
                usage=dict(api_calls=1, input_tokens=10, output_tokens=2, missing_usage_calls=0),
                target_seconds=2, query_units=1, raw_path=raw_path)
            write_json(folder/raw_path, dict(payload=self.secret))
            episodes.append(episode)
            events.extend([dict(event='TARGET_STARTED', episode_id=eid),
                           dict(event='TARGET_COMPLETED', episode_id=eid, status=episode['status'])])
            history.append(eid)
        if generation_error_at not in (None, 0):
            decide(generation_error_at, 'Revise')
            generate('stage_aware', generation_error_at, error=True)
            decide(generation_error_at, 'Stop', 'GENERATION_ERROR')
        write_jsonl(folder/'episodes.jsonl', episodes)
        write_jsonl(folder/'events.jsonl', events)
        write_jsonl(folder/'decisions.jsonl', decisions)
        return folder

    def fill(self, repeat=(False, False, False), reference=(False, False, False)):
        for cid in EXPECTED_IDS:
            self.add_case(self.run, cid, repeat)
            self.add_case(self.reference, cid, reference)

    def report(self):
        return analyze(self.run, self.reference)

    def test_independent_initials_and_reference_budget_are_explicit(self):
        self.fill()
        r = self.report()
        self.assertEqual(r['status'], 'complete')
        self.assertTrue(r['comparability']['all_recorded_checks_equal'])
        self.assertFalse(r['comparability']['budgets_identical'])
        self.assertFalse(r['comparability']['initial_identity_required'])
        self.assertFalse(r['design']['causal_effect_estimated'])
        self.assertFalse(r['design']['external_llm_seed_controlled'])
        self.assertTrue(all(c['initial_payload_hash_equal'] is False for c in r['cases']))
        self.assertEqual(r['summaries']['repeat']['assigned_cases'], 5)

    def test_c004_success_on_third_is_reproduced_after_revision(self):
        self.fill()
        for root in (self.run, self.reference):
            self.add_case(root, 'c004', (False, False, True), triggers=['F', 'F', 'T'])
        r = self.report()
        c = r['c004_reproduction']
        self.assertTrue(c['success_reproduced'])
        self.assertEqual(c['repeat']['first_success_attempt'], 3)
        self.assertTrue(c['repeat']['success_after_revision'])
        self.assertEqual(c['repeat']['success_action'], 'Revise')
        self.assertEqual(r['summaries']['repeat']['asr_at_3']['complete_asr'], .2)

    def test_initial_success_and_retry_success_are_not_called_revision_success(self):
        self.fill()
        self.add_case(self.reference, 'c004', (False, True), triggers=['F', 'T'])
        self.add_case(self.run, 'c004', (True,), triggers=['T'])
        c = self.report()['c004_reproduction']['repeat']
        self.assertEqual(c['first_success_attempt'], 1)
        self.assertFalse(c['success_after_revision'])
        self.add_case(self.run, 'c004', (False, True), triggers=['T', 'T'], actions=['Retry'])
        c = self.report()['c004_reproduction']['repeat']
        self.assertEqual(c['first_success_attempt'], 2)
        self.assertEqual(c['success_action'], 'Retry')
        self.assertFalse(c['success_after_revision'])

    def test_fourth_reference_success_and_generation_are_excluded(self):
        self.fill(reference=(False, False, False, True))
        r = self.report()
        self.assertEqual(r['summaries']['reference']['asr_at_3']['complete_asr'], 0)
        self.assertEqual(r['summaries']['reference']['cost_with_shared_initial']['generation_requests'], 15)
        self.assertEqual(r['summaries']['reference']['cost_with_shared_initial']['target_executions'], 15)
        self.assertEqual(r['cases'][0]['reference']['excluded_successes'], 1)
        self.assertEqual(len(r['cases'][0]['reference']['timeline']), 3)

    def test_failed_trigger_true_denominator_and_budget_nodes(self):
        self.fill()
        self.add_case(self.run, 'c000', (False, False, False), triggers=['T', 'T', 'T'], actions=['Retry', 'Revise'])
        self.add_case(self.run, 'c001', (False, False, None), triggers=['F', 'F', 'T'])
        r = self.report()
        d = r['summaries']['repeat']['trigger_diagnostic']
        self.assertEqual(d['valid_failed_executions'], 14)
        self.assertEqual(d['gold_false_trigger_true_executions'], 3)
        self.assertAlmostEqual(d['frequency_among_valid_failures'], 3/14)
        self.assertEqual(d['eligible_nodes_with_budget'], 2)
        self.assertEqual(d['eligible_retry_count'], 1)
        self.assertEqual(d['eligible_revise_count'], 1)
        node = r['cases'][0]['repeat']['gold_false_trigger_true_nodes'][-1]
        self.assertFalse(node['within_budget_continuation_available'])
        self.assertIsNone(node['next_action'])

    def test_no_valid_failures_gives_null_trigger_frequency(self):
        self.fill(repeat=(True,), reference=(True,))
        d = self.report()['summaries']['repeat']['trigger_diagnostic']
        self.assertEqual(d['valid_failed_executions'], 0)
        self.assertIsNone(d['frequency_among_valid_failures'])

    def test_generation_error_remains_null_and_counts_completed_failed_call(self):
        self.fill()
        self.add_case(self.reference, 'c004', (False, False, True))
        self.add_case(self.run, 'c004', (False, False), generation_error_at=2)
        r = self.report()
        c = r['c004_reproduction']
        self.assertEqual(c['status'], 'unresolved')
        self.assertIsNone(c['success_reproduced'])
        self.assertEqual(r['cases'][2]['repeat']['status'], 'GENERATION_ERROR')
        self.assertEqual(r['new_physical_cost']['generation_requests'], 15)
        self.assertEqual(r['new_physical_cost']['target_executions'], 14)
        self.assertIsNone(r['summaries']['repeat']['asr_at_3']['complete_asr'])
        self.assertEqual(r['summaries']['repeat']['assigned_cases'], 5)

    def test_initial_generation_error_is_not_mislabeled_failure(self):
        self.fill()
        self.add_case(self.run, 'c004', (), generation_error_at=0)
        r = self.report()
        row = r['cases'][2]['repeat']
        self.assertEqual(row['status'], 'GENERATION_ERROR')
        self.assertIsNone(row['gold_success'])
        self.assertEqual(r['new_physical_cost']['generation_requests'], 13)
        self.assertEqual(r['new_physical_cost']['target_executions'], 12)

    def test_missing_run_keeps_all_five_unknown(self):
        for cid in EXPECTED_IDS:
            self.add_case(self.reference, cid)
        r = analyze(self.root/'missing', self.reference)
        s = r['summaries']['repeat']['asr_at_3']
        self.assertEqual(s['confirmed_success_fraction'], 0)
        self.assertEqual(s['unknown_or_error_outcomes'], 5)
        self.assertIsNone(s['complete_asr'])
        self.assertEqual(r['status'], 'incomplete')
        self.assertFalse(r['new_physical_cost']['total_tokens_complete'])

    def test_new_cost_includes_shared_generation_and_preflight_separately(self):
        self.fill()
        write_json(self.run/'preflight.json', dict(target=dict(calls=[
            dict(stage='preflight', model='target', usage=dict(prompt_tokens=30, completion_tokens=2))])))
        r = self.report()
        cost = r['new_physical_cost']
        self.assertEqual(cost['target_completed_events'], 15)
        self.assertEqual(cost['target_executions'], 15)
        self.assertEqual(cost['generation_completed_events'], 15)
        self.assertEqual(cost['generator_model_calls'], 15)
        self.assertEqual(cost['total_tokens_known'], 15*(12+6))
        self.assertEqual(r['repeat_preflight_cost']['total_tokens_known'], 32)
        self.assertTrue(cost['total_tokens_complete'])

    def test_started_without_completion_does_not_charge_episode_cost(self):
        self.fill()
        path = self.run/'c004/events.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events = [e for e in events if not (e['event'] == 'TARGET_COMPLETED' and e.get('episode_id') == 'c004/stage_aware/002')]
        events.append(dict(event='GENERATION_STARTED', generation_id='c004/stage_aware/P3'))
        write_jsonl(path, events)
        cost = self.report()['new_physical_cost']
        self.assertEqual(cost['target_results_recorded'], 14)
        self.assertEqual(cost['target_started_events'], 15)
        self.assertEqual(cost['unresolved_target_ids'], ['c004/stage_aware/002'])
        self.assertEqual(cost['unresolved_generation_ids'], ['c004/stage_aware/P3'])
        self.assertFalse(cost['total_tokens_complete'])

    def test_comparability_detects_model_observer_policy_and_snapshot_change(self):
        self.fill()
        manifest = deepcopy(self.manifests[self.run])
        manifest['config']['target']['model'] = 'changed'
        manifest['config']['observation']['queries'] = ['changed']
        manifest['config']['policies']['stage_aware']['max_retries_per_payload'] = 4
        manifest['cases'][0]['base_task_id'] = 'changed'
        write_json(self.run/'manifest.json', manifest)
        comp = self.report()['comparability']
        self.assertFalse(comp['all_recorded_checks_equal'])
        self.assertEqual(self.report()['validation_status'], 'requires_review')
        self.assertTrue({'target', 'observation', 'stage_aware', 'c000'} <= set(comp['mismatched_checks']))

    def test_source_hashes_are_ignored_and_historical_audit_is_context_only(self):
        self.manifests[self.run]['inputs_sha256']['src/attacker/original.py'] = 'new-adapter-digest'
        write_json(self.run/'manifest.json', self.manifests[self.run])
        self.fill()
        audit = self.root/'audit.json'
        write_json(audit, dict(status='offline_audit_passed_with_documented_version_change',
            source_manifest_sha256=hashlib.sha256((self.reference/'manifest.json').read_bytes()).hexdigest(),
            changed_old_inputs=[dict(path='src/attacker/original.py', current_sha256='new-adapter-digest')],
            critical_unchanged_inputs=[dict(unchanged=True)]*22,
            tests=dict(status='passed', tests=8, api_calls=0),
            historical_prompt_reconstruction=dict(ready_generations_checked=22, prompt_calls_compared=27,
                all_prompts_and_roles_byte_identical=True, all_payload_hashes_identical=True,
                failed_generations_excluded=1, api_calls=0)))
        r = analyze(self.run, self.reference, audit)
        comp = r['comparability']
        self.assertNotIn('implementation_hashes', comp)
        self.assertTrue(comp['all_recorded_checks_equal'])
        self.assertFalse(comp['source_code_compared'])
        self.assertTrue(comp['protocol_audit']['reference_manifest_matches'])
        self.assertFalse(comp['protocol_audit']['applicable'])
        self.assertIn('该审计不认证当前实现', render_markdown(r))
        self.manifests[self.run]['inputs_sha256']['src/attacker/original.py'] = 'later-unaudited-digest'
        write_json(self.run/'manifest.json', self.manifests[self.run])
        self.assertFalse(analyze(self.run, self.reference, audit)['comparability']['protocol_audit']['applicable'])

    def test_reference_continuations_replay_equally_with_two_post_failure_budget(self):
        self.fill()
        r = self.report()
        replay = r['comparability']['reference_budget_prefix_replay']
        self.assertEqual(replay['status'], 'verified')
        self.assertEqual(replay['decisions_replayed'], 10)
        self.assertTrue(replay['all_recorded_actions_and_reasons_match'])
        self.assertFalse(replay['source_code_compared'])
        self.assertEqual(replay['api_calls'], 0)
        self.assertIn('前两次继续决策在这两个预算下相同', render_markdown(r))
        first = r['cases'][0]['reference']['budget_prefix_replay']['decisions'][0]
        self.assertEqual(first['old_budget'], 3)
        self.assertEqual(first['repeat_budget'], 2)
        self.assertEqual(first['old_budget_action'], 'Revise')
        self.assertEqual(first['repeat_budget_action'], 'Revise')

    def test_reference_replay_mismatch_is_visible_and_not_called_equivalent(self):
        self.fill()
        path = self.reference/'c000/decisions.jsonl'
        decisions = [json.loads(line) for line in path.read_text().splitlines()]
        decisions[0]['action'] = 'Retry'
        write_jsonl(path, decisions)
        r = self.report()
        replay = r['comparability']['reference_budget_prefix_replay']
        self.assertEqual(replay['status'], 'requires_review')
        self.assertFalse(replay['all_recorded_actions_and_reasons_match'])
        self.assertEqual(r['validation_status'], 'requires_review')
        self.assertNotIn('前两次继续决策在这两个预算下相同', render_markdown(r))

    def test_reference_replay_missing_history_remains_unverified(self):
        self.fill()
        path = self.reference/'c000/episodes.jsonl'
        episodes = [json.loads(line) for line in path.read_text().splitlines()]
        del episodes[0]['stage']
        write_jsonl(path, episodes)
        r = self.report()
        replay = r['comparability']['reference_budget_prefix_replay']
        self.assertEqual(replay['status'], 'requires_review')
        self.assertEqual(replay['decisions_replayed'], 8)
        first = r['cases'][0]['reference']['budget_prefix_replay']['decisions'][0]
        self.assertFalse(first['replayed'])
        self.assertEqual(first['error_type'], 'TypeError')

    def test_cli_outputs_metadata_without_payload_text(self):
        self.fill()
        prefix = self.root/'reports/repeat'
        self.assertEqual(main(['--run', str(self.run), '--reference-run', str(self.reference),
                               '--outputprefix', str(prefix)]), 0)
        j, md = prefix.with_suffix('.json').read_text(), prefix.with_suffix('.md').read_text()
        self.assertNotIn(self.secret, j+md)
        self.assertIn('TotalASR@3', md)
        self.assertIn('不估计配对因果效应', md)
        self.assertIn('不能当固定 Payload 重放', md)
        self.assertIn('非精确计费/上下文长度', md)
        self.assertIn('外部生成模型', md)


if __name__ == '__main__':
    unittest.main()
