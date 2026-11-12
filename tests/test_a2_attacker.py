"""A2 resource fidelity and shared Evidence Runner integration, entirely offline."""
import ast
from copy import deepcopy
import inspect
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from src.engine.config import ROOT, load_cases, load_config, freeze, check_frozen, validate
from src.engine.runner import Runner
from src.engine.storage import RunStore
from src.evidence.observer import FixedObserver
from src.policies.registry import build_policies
from src.schema import TargetResult
from src.vendor import router
from src.vendor.a2_selection import select_cases


class A2SelectionTests(unittest.TestCase):
    """Selection invariants inherited from MiniPIM, using inert synthetic cases."""

    def test_selection_and_partition_are_verbatim_upstream_functions(self):
        from src.vendor import a2_selection
        for filename, names in [('raw_ablation.py', ('select_cases',)),
                                ('library_token_audit.py', ('partition',))]:
            source = (ROOT / 'strategy_library_a2/upstream' / filename).read_text()
            definitions = {n.name: ast.get_source_segment(source, n)
                           for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}
            for name in names:
                self.assertEqual(inspect.getsource(getattr(a2_selection, name)).rstrip(),
                                 definitions[name], name)

    @staticmethod
    def example(identifier, *, scenario='banking', task_type='information_retrieval',
                group=None, strategy='s', outcome='unknown'):
        return dict(case_id=identifier, strategy_id=strategy, scenario=scenario,
                    task_type=task_type, group=group or ['agentdojo', scenario, identifier],
                    outcome_label=outcome, raw='RAW-' + identifier + '\n' + 'x' * 10000)

    def select(self, cases, scores=None, **kwargs):
        options = dict(strategy_id='s', scenario='banking', task_type='information_retrieval',
                       current_group=['agentdojo', 'banking', 'current'],
                       scores=scores or {c['case_id']: 0.5 for c in cases}, k=2)
        options.update(kwargs)
        return select_cases(cases, **options)

    def test_group_exclusion_precedes_fallback_and_raw_text_survives(self):
        current = self.example('current', group=['agentdojo', 'banking', 'current'])
        near = self.example('near')
        other_suite = self.example('cross', scenario='slack')
        wrong_type = self.example('wrong', task_type='financial_operation')
        wrong_strategy = self.example('foreign', strategy='other')
        cases = [current, near, other_suite, wrong_strategy, wrong_type]
        before = deepcopy(cases)
        selected, log = self.select(cases, {'current': 1, 'near': .2, 'cross': .9,
                                           'wrong': 1, 'foreign': 1})
        self.assertEqual([c['case_id'] for c in selected], ['cross', 'near'])
        self.assertEqual(log['fallback_tier'], 'same_type_any_scenario')
        self.assertEqual(log['eligible_count'], 2)
        self.assertEqual(cases, before)
        self.assertEqual(selected[0]['raw'], other_suite['raw'])
        self.assertGreater(len(selected[0]['raw']), 10000)

    def test_sufficient_matching_pool_never_leaks_better_other_suite_score(self):
        cases = [self.example('b'), self.example('a'), self.example('cross', scenario='slack')]
        selected, log = self.select(cases, {'a': .4, 'b': .4, 'cross': 1})
        self.assertEqual([c['case_id'] for c in selected], ['a', 'b'])
        self.assertEqual(log['fallback_tier'], 'same_scenario_and_type')

    def test_final_fallback_has_no_padding_or_duplicate_current_case(self):
        cases = [self.example('current', group=['agentdojo', 'banking', 'current']),
                 self.example('only', scenario='travel', task_type='booking_operation')]
        selected, log = self.select(cases)
        self.assertEqual([c['case_id'] for c in selected], ['only'])
        self.assertEqual(log['fallback_tier'], 'any_case_of_strategy')
        self.assertEqual((log['loaded'], log['shortfall']), (1, 1))
        selected, log = self.select(cases[:1])
        self.assertEqual(selected, [])
        self.assertEqual((log['loaded'], log['shortfall']), (0, 2))

    def test_known_success_preference_and_recovered_inclusion_use_situation_order(self):
        cases = [self.example('s1', outcome='success'), self.example('s2', outcome='success'),
                 self.example('r', outcome='recovered'), self.example('u'),
                 self.example('f', outcome='failure')]
        selected, log = self.select(cases, {'s1': .3, 's2': .2, 'r': .4, 'u': .99, 'f': 1})
        self.assertEqual([c['case_id'] for c in selected], ['r', 's1'])
        self.assertTrue(log['recovered_available'])
        self.assertEqual(log['known_outcomes'], 4)

    def test_missing_type_cannot_silently_change_candidate_filtering(self):
        with self.assertRaisesRegex(ValueError, 'task_type'):
            self.select([self.example('a')], task_type=None)
        with self.assertRaisesRegex(ValueError, 'Unlabelled'):
            self.select([self.example('a', task_type=None)])


class A2AdapterTests(unittest.TestCase):
    def setUp(self):
        from src.attacker.a2 import A2PIMinerAdapter, A2Resources
        self.adapter_type = A2PIMinerAdapter
        self.resources_type = A2Resources
        self.library_path = 'strategy_library_a2/library.json'
        self.retrieval_path = 'data/a2/dev.retrieval.json'
        self.config = load_config(ROOT / 'experiments/e0_native_dev.yaml')
        self.config['attacker'].update(mode='a2', a2_library=self.library_path,
                                       a2_retrieval=self.retrieval_path)
        self.case = load_cases(self.config)[0]
        self.strategies = router._discover_strategies('agentdojo')
        self.ids = [s['id'] for s in self.strategies[:3]]

    def adapter(self, decide):
        return self.adapter_type(ROOT, 'fixture', 'target-fixture',
                                 self.library_path, self.retrieval_path, decide=decide)

    def test_mode_wiring_and_original_default(self):
        from main import adapters
        from src.attacker.original import OriginalPIMinerAdapter
        validate(self.config)
        with patch('src.adapters.agentdojo.AgentDojoAdapter'):
            _, attacker = adapters(self.config)
            self.assertIsInstance(attacker, self.adapter_type)
            self.assertFalse(attacker.use_cached_p0)
            original = load_config(ROOT / 'experiments/e0_native_dev.yaml')
            _, attacker = adapters(original)
            self.assertIsInstance(attacker, OriginalPIMinerAdapter)
            self.assertNotIsInstance(attacker, self.adapter_type)
        fixture = deepcopy(self.config)
        fixture['backend'] = 'fixture'
        with self.assertRaises(ValueError):
            validate(fixture)

    def test_stale_public_case_is_rejected_before_model_call(self):
        from src.attacker.piminer import GenerationError
        stale = deepcopy(self.case)
        stale['user_task_prompt'] += '\nChanged public task.'
        calls = []
        attacker = self.adapter(lambda prompt, stage: calls.append(stage))
        with self.assertRaises(GenerationError):
            attacker.generate(stale, None, None, (), 'P0')
        self.assertEqual(calls, [])
        def execute(*args):
            self.fail('A stale A2 retrieval must fail before any Target execution')
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(types.SimpleNamespace(execute=execute), attacker, FixedObserver(),
                            build_policies(self.config['policies']),
                            RunStore(Path(directory) / 'run', {}))
            metrics = runner.run([stale])
        self.assertEqual(calls, [])
        self.assertEqual(metrics['physical_total_cost']['target_executions'], 0)
        self.assertEqual(metrics['physical_total_cost']['generator_model_calls'], 0)
        self.assertEqual(runner.generations[0]['mode'], 'a2')
        self.assertTrue(all(o['status'] == 'GENERATION_ERROR' for o in runner.outcomes))

    def test_evidence_runner_shares_new_p0_and_keeps_selection_and_histories_local(self):
        seen, executions = [], []
        count = 0

        def decide(prompt, stage):
            nonlocal count
            seen.append((stage, prompt))
            if stage == 'router':
                return '<choice>' + ', '.join(self.ids) + '</choice>'
            count += 1
            return f'<analysis>analysis-{count}</analysis><injection>candidate-{count}</injection>'

        def execute(case, payload, eid):
            executions.append((eid, payload.text))
            return TargetResult(False, final='plain final', utility=True,
                                black_box_final='VISIBLE-' + eid,
                                raw={'secret': 'PRIVATE_TRACE'})

        case = deepcopy(self.case)
        case['private_judge'] = 'PRIVATE_JUDGE'
        case['future_history'] = 'PRIVATE_FUTURE'
        with tempfile.TemporaryDirectory() as directory:
            attacker = self.adapter(decide)
            runner = Runner(types.SimpleNamespace(execute=execute), attacker,
                            FixedObserver(), build_policies(self.config['policies']),
                            RunStore(Path(directory) / 'run', {}))
            metrics = runner.run([case])
            self.assertTrue(runner.generations)
            self.assertTrue(all(g['status'] == 'ready' for g in runner.generations))
            first = runner.generations[0]
            self.assertEqual(first['mode'], 'a2')
            self.assertEqual(first['routing']['strategy_ids'], self.ids)
            selected = first['routing']['a2']['selections']
            self.assertEqual(set(selected), set(self.ids))
            self.assertTrue(all(len(s['case_ids']) <= 2 for s in selected.values()))
            for generation in runner.generations:
                self.assertEqual(generation['routing']['a2']['selections'], selected)
                self.assertEqual(generation['mode'], 'a2')
                for call in generation['calls']:
                    self.assertNotIn('PRIVATE_', call['prompt'])
                    self.assertNotIn('Requested attacker observations', call['prompt'])
                if generation['policy_id'] == 'shared':
                    continue
                prompt = generation['calls'][0]['prompt']
                self.assertEqual(generation['calls'][0]['stage'], 'attacker_revise')
                self.assertIn('VISIBLE-' + case['case_id'] + '/shared/000', prompt)
                for other in self.config['policies']:
                    if other != generation['policy_id']:
                        self.assertNotIn('VISIBLE-' + case['case_id'] + '/' + other + '/', prompt)
            stage = next(g for g in runner.generations if g['policy_id'] == 'stage_aware')
            self.assertEqual(stage['calls'][0]['prompt'].count('### Attempt '), 2)
            self.assertEqual(stage['calls'][0]['prompt'].count('analysis-1'), 2)
            third = next(g for g in runner.generations
                         if g['policy_id'] == 'always_revise' and g['generation_id'].endswith('/P3'))
            self.assertEqual(third['calls'][0]['prompt'].count('### Attempt '), 3)
        self.assertEqual([s for s, _ in seen].count('router'), 1)
        self.assertEqual([s for s, _ in seen].count('attacker_p0'), 1)
        self.assertEqual(executions[0][1], 'candidate-1')
        self.assertNotEqual(executions[0][1], case['p0'])
        self.assertEqual({p for eid, p in executions if '/always_retry/' in eid}, {'candidate-1'})
        self.assertEqual(metrics['physical_total_cost']['generator_calls_by_stage']['router'], 1)
        self.assertEqual(metrics['policies']['always_retry']['physical_post_failure_cost']['generator_model_calls'], 0)

    def test_a2_runner_consumes_observer_evidence_for_controller_only(self):
        from src.adapters.fixture import FixtureTarget
        seen = []
        def decide(prompt, stage):
            seen.append((stage, prompt))
            if stage == 'router':
                return '<choice>' + ', '.join(self.ids) + '</choice>'
            return '<analysis>fixture analysis</analysis><injection>fixture candidate</injection>'
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(FixtureTarget(), self.adapter(decide), FixedObserver(),
                            build_policies(self.config['policies']),
                            RunStore(Path(directory) / 'run', {}))
            metrics = runner.run([self.case])
        self.assertTrue(runner.episodes)
        self.assertTrue(any(runner.packets.values()))
        self.assertGreater(metrics['physical_total_cost']['observation_query_units'], 0)
        decisions = [d for d in runner.decisions if d['policy_id'] == 'stage_aware']
        self.assertTrue(any(d['action'] == 'Retry' and d['reason'] == 'preserve_observed_trigger'
                            for d in decisions))
        self.assertEqual([s for s, _ in seen].count('router'), 1)
        for stage, prompt in seen:
            self.assertNotIn('Requested attacker observations', prompt)
            self.assertNotIn('query_packets', prompt)
            self.assertNotIn('received_attachment_counts', prompt)

    def test_freeze_covers_a2_resources_and_entire_original_router_library(self):
        manifest = freeze(self.config)
        resources = self.resources_type(ROOT, self.library_path, self.retrieval_path)
        required = {str(Path(p).relative_to(ROOT)) for p in resources.input_paths}
        required |= {str(p.relative_to(ROOT)) for p in (ROOT / 'strategy_library').rglob('*')
                     if p.is_file()}
        required.add('src/vendor/attacker_role.txt')
        self.assertTrue(required.issubset(manifest['inputs_sha256']))
        for path in (self.library_path, self.retrieval_path):
            changed = deepcopy(manifest)
            changed['inputs_sha256'][path] = 'changed'
            with self.assertRaisesRegex(ValueError, 'bytes changed'):
                check_frozen(changed)

    def test_original_router_and_role_with_lossless_upstream_a2_rendering(self):
        from src.attacker.piminer import PUBLIC_FIELDS
        from src.vendor.prompts import build_user_message_blocks
        resources = self.resources_type(ROOT, self.library_path, self.retrieval_path)
        document, metadata = resources.compose(self.case, self.strategies[:3])
        library = json.loads((ROOT / self.library_path).read_text())
        by_id = {c['case_id']: c for c in library['cases']}
        source = (ROOT / 'strategy_library_a2/upstream/raw_ablation.py').read_text()
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'render_variant')
        namespace = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'upstream_render', 'exec'), namespace)
        selected = {sid: [by_id[cid] for cid in metadata['selections'][sid]['case_ids']]
                    for sid in self.ids}
        texts = namespace['render_variant'](
            {sid: library['strategies'][sid] for sid in self.ids}, selected, 'A2')
        expected = '\n\n'.join(f'===== Candidate strategy {i}/{len(self.ids)}: {sid} =====\n\n{texts[sid]}'
                               for i, sid in enumerate(self.ids, 1))
        self.assertEqual(document, expected)
        self.assertEqual(metadata['strategy_document_sha256'],
                         hashlib.sha256(document.encode()).hexdigest())
        for sid in self.ids:
            self.assertIn(library['strategies'][sid]['method'], document)
            for example in selected[sid]:
                self.assertIn(example['raw'], document)
                self.assertNotEqual(example['group'],
                                    ['agentdojo', self.case['suite_name'], self.case['user_task_id']])
            omitted = [c for c in library['cases'] if c['strategy_id'] == sid and c not in selected[sid]]
            for example in omitted:
                self.assertNotIn(example['raw'], texts[sid])
        self.assertLess(len(document), metadata['original_document_chars'])
        seen = []

        def decide(prompt, stage):
            seen.append((stage, prompt))
            if stage == 'router':
                return '<choice>' + ', '.join(self.ids) + '</choice>'
            return '<analysis>original-role-analysis</analysis><injection>fresh-A2</injection>'

        attacker = self.adapter(decide)
        payload, record = attacker.generate(self.case, None, None, ('PRIVATE_EVIDENCE',), 'P0')
        public = {key: self.case[key] for key in PUBLIC_FIELDS}
        self.assertEqual(seen[0][1], router.ROUTER_ROLE_PROMPT + '\n\n' +
                         router._format_routing_prompt('target-fixture', public, self.strategies))
        self.assertEqual(seen[1][1], '\n'.join(build_user_message_blocks(
            expected, public, [], threat_model='black_box')))
        self.assertEqual(record['calls'][1]['system_prompt'],
                         (ROOT / 'src/vendor/attacker_role.txt').read_text())
        history = [dict(iter=0, analysis='original-role-analysis', injection=payload.text,
                        security=False, utility=True, final_output='visible final',
                        trajectory='PRIVATE_TRACE')]
        attacker.generate_with_history(self.case, payload,
            types.SimpleNamespace(gold=False, error=None), ('PRIVATE_EVIDENCE',), 'P1', history)
        self.assertNotIn('PRIVATE_', seen[-1][1])
        self.assertEqual([s for s, _ in seen], ['router', 'attacker_p0', 'attacker_revise'])

    def test_tampered_selection_cache_and_library_bytes_are_rejected(self):
        retrieval = json.loads((ROOT / self.retrieval_path).read_text())
        library = json.loads((ROOT / self.library_path).read_text())
        with tempfile.TemporaryDirectory(prefix='a2-test-', dir=ROOT) as directory:
            temporary = Path(directory)
            bad_retrieval = deepcopy(retrieval)
            selections = bad_retrieval['tasks'][self.case['case_id']]['selections']
            sid = next(sid for sid, selection in selections.items() if selection['case_ids'])
            selections[sid]['case_ids'] = []
            retrieval_path = temporary / 'retrieval.json'
            retrieval_path.write_text(json.dumps(bad_retrieval))
            resources = self.resources_type(ROOT, self.library_path, retrieval_path)
            with self.assertRaisesRegex(ValueError, 'selection cache'):
                resources.validate_case(self.case)
            library_path = temporary / 'library.json'
            sid = next(iter(library['strategies']))
            library['strategies'][sid]['method'] += '\nchanged method'
            library_path.write_text(json.dumps(library))
            with self.assertRaisesRegex(ValueError, 'library hash mismatch'):
                self.resources_type(ROOT, library_path, self.retrieval_path)
            # Updating the declared hash cannot hide lost or rewritten source bytes.
            retrieval['library_sha256'] = hashlib.sha256(library_path.read_bytes()).hexdigest()
            retrieval_path.write_text(json.dumps(retrieval))
            with self.assertRaisesRegex(ValueError, 'method text differs from source'):
                self.resources_type(ROOT, library_path, retrieval_path)


if __name__ == '__main__':
    unittest.main()
