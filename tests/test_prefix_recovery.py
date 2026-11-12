"""Real completed-prefix recovery and failure-closed audits, entirely offline."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import types
import unittest

from src.attacker.aligned import PIMinerAlignedAdapter
from src.engine.config import ROOT, check_frozen, data_fingerprints, freeze
from src.engine.prefix_recovery import RecoveryPrefix, PrefixRecoveryRunner, ORIGIN
from src.engine.storage import RunStore
from src.evidence.observer import FixedObserver
from src.policies.registry import build_policies
from src.schema import Action, Decision, TargetResult

SOURCE = ROOT / 'runs/evidence_piminer_aligned_asr5_20261030'
RECOVERY_MANIFEST = ROOT / 'runs/evidence_piminer_aligned_asr5_20261030_recovered_v2/manifest.json'


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.chmod(0o600)
    path.write_text(json.dumps(value, ensure_ascii=False) + '\n')


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_rows(path, values):
    Path(path).write_text(''.join(json.dumps(value, ensure_ascii=False) + '\n' for value in values))


class FakeSession:
    calls_created = []

    def __init__(self, **options):
        self.calls = []
        self.session_id = 'offline-session-' + str(len(self.calls_created))
        self.calls_created.append(self)

    def __call__(self, prompt, stage):
        self.calls.append(dict(stage=stage, usage={'input_tokens': 0, 'output_tokens': 0}))
        return '<analysis>offline fixture analysis</analysis><injection>offline fixture candidate</injection>'


class FakeTarget:
    def __init__(self, prefix_counts):
        self.prefix_counts, self.calls = prefix_counts, []

    def execute(self, case, payload, episode_id):
        ordinal = int(episode_id.rsplit('/', 1)[1])
        if ordinal < self.prefix_counts[case['case_id']]:
            raise AssertionError('Recovered target execution was sent again')
        self.calls.append(episode_id)
        return TargetResult(False, final='offline continuation', utility=False)


class CountingObserver(FixedObserver):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = []

    def collect(self, episode_id, case, view):
        self.calls.append(episode_id)
        return super().collect(episode_id, case, view)


class PrefixRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep the historical data/configuration snapshot. Python source hashes
        # inside this legacy manifest are deliberately ignored by recovery.
        cls.current_manifest = read(RECOVERY_MANIFEST)
        cls.config = deepcopy(cls.current_manifest['config'])
        cls.cases = deepcopy(cls.current_manifest['cases'])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        FakeSession.calls_created = []

    def copied_source(self):
        source = self.directory / 'source'
        shutil.copytree(SOURCE, source)
        for case in self.cases:
            cid = case['case_id']
            manifest = read(source / cid / 'manifest.json')
            manifest['parent_run'] = str(source)
            write(source / cid / 'manifest.json', manifest)
            events = rows(source / cid / 'events.jsonl')
            events[-1]['path'] = str(source / 'experience/stage_aware/intra_dataset_experience.jsonl')
            write_rows(source / cid / 'events.jsonl', events)
        return source

    def runner(self, recovery, *, policies=None, runner_type=PrefixRecoveryRunner):
        target = FakeTarget(recovery.metadata['case_prefix_counts'])
        observer = CountingObserver(**self.config['observation'])
        attacker = PIMinerAlignedAdapter(ROOT,
            model=self.config['attacker']['model'], target_model=self.config['target']['model'],
            initialization_manifest=self.config['attacker']['initialization_manifest'],
            session_factory=FakeSession)
        store = RunStore(self.directory / 'recovered', dict(self.current_manifest, recovery=recovery.metadata))
        runner = runner_type(target, attacker, observer,
            policies or build_policies(self.config['policies']), store,
            post_failure_budget=self.config['post_failure_target_budget'], seed=self.config['seed'], recovery=recovery)
        return runner, target, observer

    def test_real_25_prefixes_recover_exact_bytes_and_use_only_85_new_target_slots(self):
        recovery = RecoveryPrefix(SOURCE, self.current_manifest)
        self.assertEqual(recovery.metadata['prefix_target_executions'], 25)
        self.assertEqual({cid for cid, count in recovery.metadata['case_prefix_counts'].items() if count == 2},
                         {'c011', 'c025', 'c028'})
        historical_metadata = {key: value for key, value in self.current_manifest['recovery'].items()
                               if key not in ('changed_input_paths', 'added_input_paths')}
        self.assertEqual(recovery.metadata, historical_metadata)
        self.assertNotIn('changed_input_paths', recovery.metadata)
        self.assertNotIn('added_input_paths', recovery.metadata)
        self.assertEqual(recovery.metadata, RecoveryPrefix(SOURCE, self.current_manifest).metadata)
        runner, target, observer = self.runner(recovery)
        metrics = runner.run(deepcopy(self.cases))
        self.assertEqual(len(target.calls), 85)
        self.assertEqual(observer.calls, target.calls)
        self.assertEqual(metrics['physical_total_cost']['target_executions'], 110)
        self.assertEqual(metrics['recovered_prefix_target_executions'], 25)
        self.assertEqual(metrics['new_target_executions'], 85)
        self.assertTrue(all(outcome['status'] == 'VALID_GOLD_FALSE' for outcome in runner.outcomes))
        self.assertEqual(len(runner.outcomes), 22)
        for case in self.cases:
            records = recovery.for_case(case)
            for record in records:
                episode = record['episode']
                path = runner.store.path / episode['raw_path']
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), record['source_raw_sha256'])
                self.assertEqual(path.read_bytes(), (SOURCE / case['case_id'] / episode['raw_path']).read_bytes())
                feedback = runner.attempts[episode['episode_id']]
                result = record['raw']['result']
                self.assertEqual(feedback['analysis'], record['analysis'])
                self.assertEqual(feedback['security'], result['gold'])
                self.assertEqual(feedback['utility'], result['utility'])
                self.assertEqual(feedback['final_output'], (result['black_box_final'] if result['black_box_final'] is not None else result['final'])[:4000])
                self.assertEqual(list(runner.packets[episode['episode_id']]), record['raw']['query_packets'])
            revision = next(g for g in runner.generations if g['generation_id'] == case['case_id'] + '/stage_aware/P1')
            self.assertEqual(revision['history_iters'], list(range(len(records))))
            self.assertEqual(revision['prompt_mode'], 'full')
        events = rows(runner.store.path / 'events.jsonl')
        recovered = [event for event in events if event['event'] == 'PREFIX_EXECUTION_RECOVERED']
        self.assertEqual(len(recovered), 25)
        self.assertTrue(all(event['record_origin'] == ORIGIN for event in recovered))
        self.assertFalse(any('REUSED' in event['event'] for event in events))
        self.assertFalse(any(g['status'] == 'GENERATION_ERROR' for g in runner.generations))
        memory = rows(runner.store.path / 'experience/stage_aware/intra_dataset_experience.jsonl')
        self.assertEqual(len(memory), 22)
        self.assertTrue(all(entry['_terminal_status'] == 'VALID_GOLD_FALSE' for entry in memory))
        recovery.assert_immutable()

    def test_current_code_and_legacy_source_hash_changes_do_not_block_recovery(self):
        current_source_manifest = freeze(deepcopy(self.config))
        self.assertEqual(data_fingerprints(current_source_manifest['inputs_sha256']),
                         data_fingerprints(self.current_manifest['inputs_sha256']))
        current_recovery = RecoveryPrefix(SOURCE, current_source_manifest)
        self.assertEqual(current_recovery.metadata['prefix_target_executions'], 25)
        # Arbitrary changed, added and removed Python entries have no bearing on
        # data identity, whether a manifest uses the old or new schema.
        legacy = deepcopy(self.current_manifest)
        code_paths = [path for path in legacy['inputs_sha256'] if Path(path).suffix == '.py']
        self.assertTrue(code_paths)
        for path in code_paths:
            legacy['inputs_sha256'][path] = 'changed-source-not-validated'
        legacy['inputs_sha256'].pop(code_paths[0])
        legacy['inputs_sha256']['another_checkout/new_source.py'] = 'new-source-not-validated'
        self.assertEqual(RecoveryPrefix(SOURCE, legacy).metadata, current_recovery.metadata)
        self.assertEqual(check_frozen(legacy), self.cases)
        self.assertEqual(check_frozen(self.current_manifest), self.cases)

    def test_source_requires_identical_config_environment_cases_and_data_resources(self):
        dataset = self.config['dataset']
        resource = next(path for path in self.current_manifest['inputs_sha256']
                        if Path(path).suffix in ('.md', '.txt'))
        for field in ('config', 'environment', 'case_ids', 'changed_data', 'missing_data', 'new_data', 'resource'):
            manifest = deepcopy(self.current_manifest)
            if field == 'config':
                manifest['config']['post_failure_target_budget'] = 3
            elif field == 'environment':
                manifest['environment']['python'] = 'changed'
            elif field == 'case_ids':
                manifest['case_ids'].reverse()
            elif field == 'changed_data':
                manifest['inputs_sha256'][dataset] = 'changed-data'
            elif field == 'missing_data':
                manifest['inputs_sha256'].pop(dataset)
            elif field == 'resource':
                manifest['inputs_sha256'][resource] = 'changed-resource'
            else:
                manifest['inputs_sha256']['data/unapproved.json'] = 'new-data'
            with self.subTest(field=field), self.assertRaises(ValueError):
                RecoveryPrefix(SOURCE, manifest)
            if field in ('changed_data', 'missing_data', 'new_data', 'resource'):
                with self.subTest(check_frozen=field), self.assertRaises(ValueError):
                    check_frozen(manifest)

    def test_source_parent_or_child_incomplete_cannot_recover(self):
        source = self.copied_source()
        for relative in ('status.json', 'c000/status.json'):
            path = source / relative
            old = read(path)
            changed = dict(old, status='running')
            write(path, changed)
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                RecoveryPrefix(source, self.current_manifest)
            write(path, old)

    def test_model_called_unknown_error_and_successful_source_cannot_recover(self):
        source = self.copied_source()
        generation = source / 'c000/raw/c000/stage_aware/P1.generation.json'
        old = read(generation)
        for key, value in [('calls', [{'stage': 'attacker_revise'}]), ('error_type', 'TimeoutError'),
                           ('session_id', 'uncertain-session'), ('payload', 'later candidate')]:
            write(generation, dict(old, **{key: value}))
            with self.subTest(key=key), self.assertRaises(ValueError):
                RecoveryPrefix(source, self.current_manifest)
        write(generation, old)
        episode_path = source / 'c000/episodes.jsonl'
        episodes = rows(episode_path)
        episodes[0].update(gold=True, status='VALID_GOLD_TRUE')
        write_rows(episode_path, episodes)
        with self.assertRaises(ValueError):
            RecoveryPrefix(source, self.current_manifest)

    def test_unknown_unmatched_duplicate_events_and_partial_prefix_cannot_recover(self):
        source = self.copied_source()
        path = source / 'c011/events.jsonl'
        old = rows(path)
        for changed in (old + [{'event': 'UNKNOWN'}], old[:-2], old + [old[2]],
                        old[:4] + old[6:]):
            write_rows(path, changed)
            with self.assertRaises(ValueError):
                RecoveryPrefix(source, self.current_manifest)
        write_rows(path, old)
        episodes_path = source / 'c011/episodes.jsonl'
        write_rows(episodes_path, rows(episodes_path)[:1])
        with self.assertRaises(ValueError):
            RecoveryPrefix(source, self.current_manifest)

    def test_any_session_or_published_memory_makes_resume_uncertain(self):
        source = self.copied_source()
        session = source / 'c000/sessions'
        session.mkdir()
        with self.assertRaisesRegex(ValueError, 'model session'):
            RecoveryPrefix(source, self.current_manifest)
        session.rmdir()
        memory = source / 'experience/stage_aware/intra_dataset_experience.jsonl'
        memory.write_text('{}\n')
        with self.assertRaisesRegex(ValueError, 'memory'):
            RecoveryPrefix(source, self.current_manifest)

    def test_raw_and_events_remain_immutable_after_loading(self):
        source = self.copied_source()
        for relative in ('c000/raw/c000/shared/000.json', 'c000/events.jsonl'):
            recovery = RecoveryPrefix(source, self.current_manifest)
            path = source / relative
            data = path.read_bytes()
            path.write_bytes(data + b'\n')
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, 'bytes changed'):
                recovery.for_case(self.cases[0])
            path.write_bytes(data)
        recovery = RecoveryPrefix(source, self.current_manifest)
        wrong_case = dict(self.cases[0], user_task_id='wrong-case')
        with self.assertRaisesRegex(ValueError, 'identity drift'):
            recovery.for_case(wrong_case)

    def test_payload_or_ordinal_mismatch_never_falls_back_to_target(self):
        class WrongOrdinalRunner(PrefixRecoveryRunner):
            def execute(self, case, policy_id, payload, phase, ordinal):
                return super().execute(case, policy_id, payload, phase, ordinal + 1)
        recovery = RecoveryPrefix(SOURCE, self.current_manifest)
        runner, target, observer = self.runner(recovery, runner_type=WrongOrdinalRunner)
        with self.assertRaisesRegex(ValueError, 'does not match the frozen prefix'):
            runner.run([deepcopy(self.cases[0])])
        self.assertEqual(target.calls, [])
        self.assertEqual(observer.calls, [])
        self.assertEqual(FakeSession.calls_created, [])
        self.assertEqual(runner.outcomes, [])

    def test_policy_cannot_revise_before_consuming_retry_prefix(self):
        recovery = RecoveryPrefix(SOURCE, self.current_manifest)
        case = next(c for c in self.cases if c['case_id'] == 'c011')
        policies = {'stage_aware': types.SimpleNamespace(decide=lambda history: Decision(Action.REVISE, 'premature'))}
        runner, target, observer = self.runner(recovery, policies=policies)
        with self.assertRaisesRegex(ValueError, 'policy diverged'):
            runner.run([deepcopy(case)])
        self.assertEqual(len(runner.episodes), 1)
        self.assertEqual(target.calls, [])
        self.assertEqual(observer.calls, [])
        self.assertEqual(FakeSession.calls_created, [])
        self.assertEqual(runner.outcomes, [])


if __name__ == '__main__':
    unittest.main()
