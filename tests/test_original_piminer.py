"""Original-source equivalence and controller integration; no remote model calls."""
import ast
from copy import deepcopy
import inspect
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from src.attacker.original import OriginalPIMinerAdapter
from src.engine.config import ROOT, load_cases, load_config, freeze, check_frozen, validate
from src.engine.runner import Runner
from src.engine.storage import RunStore
from src.evidence.observer import FixedObserver
from src.policies.registry import build_policies
from src.schema import TargetResult
from src.vendor import router, prompts, final_output


def original_functions(filename, names, namespace):
    source = (ROOT / 'data/original/piminer_code' / filename).read_text()
    nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec'), namespace)
    return namespace


class OriginalPIMinerTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / 'experiments/e0_smoke.yaml')
        self.case = load_cases(self.config)[0]
        self.strategies = router._discover_strategies('agentdojo')
        self.ids = [s['id'] for s in self.strategies[:3]]

    def test_extracted_functions_and_router_constants_are_verbatim(self):
        pairs = [('iterative_attack_claude_code.py', router),
                 ('iterative_attack_common.py', prompts),
                 ('iterative_attack_common.py', final_output)]
        for filename, module in pairs:
            source = (ROOT / 'data/original/piminer_code' / filename).read_text()
            definitions = {n.name: ast.get_source_segment(source, n) for n in ast.parse(source).body
                           if isinstance(n, ast.FunctionDef)}
            for name, value in vars(module).items():
                if inspect.isfunction(value) and value.__module__ == module.__name__:
                    self.assertEqual(inspect.getsource(value).rstrip(), definitions[name], name)
        source = (ROOT / 'data/original/piminer_code/iterative_attack_claude_code.py').read_text()
        constants = ['ROUTER_ROLE_PROMPT', 'ROUTE_TOP_K', 'COLD_START_SEED_ID', 'SCOPE_HEADINGS']
        nodes = [n for n in ast.parse(source).body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets)]
        namespace = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'original_constants', 'exec'), namespace)
        for name in constants:
            self.assertEqual(getattr(router, name), namespace[name])
        step = (ROOT / 'data/original/piminer_code/step.md').read_text()
        expected = step.split('# Your role (the attacker)\n\n', 1)[1].split('# Procedure (this iteration)', 1)[0]
        self.assertEqual((ROOT / 'src/vendor/attacker_role.txt').read_text(), expected)

    def test_prompts_match_original_source_and_all_routed_examples_are_retained(self):
        seen = []
        def decide(prompt, stage):
            seen.append((stage, prompt))
            if stage == 'router':
                return '<choice>' + ', '.join(self.ids) + '</choice>'
            return '<analysis>First analysis</analysis><injection>new P0</injection>'
        adapter = OriginalPIMinerAdapter(ROOT, 'fixture', 'target-fixture', decide=decide)
        payload, record = adapter.generate(self.case, None, None, (), 'P0')
        reference = original_functions('iterative_attack_claude_code.py',
            ['_format_routing_prompt', '_load_sample_strategy'], dict(vars(router)))
        expected = router.ROUTER_ROLE_PROMPT + '\n\n' + reference['_format_routing_prompt'](
            'target-fixture', self.case, self.strategies)
        self.assertEqual(seen[0], ('router', expected))
        paths = [s['path'] for s in self.strategies[:3]]
        document = reference['_load_sample_strategy'](ROOT, dict(strategy_paths=paths), None)
        reference = original_functions('iterative_attack_common.py',
            ['_format_attempt', 'build_user_message_blocks'], dict(vars(prompts)))
        self.assertEqual(seen[1][1], '\n'.join(reference['build_user_message_blocks'](
            document, self.case, [], threat_model='black_box')))
        for path in paths:
            self.assertIn(Path(path).read_text(), seen[1][1])
        self.assertEqual(payload.text, 'new P0')
        self.assertEqual(record['analysis'], 'First analysis')
        self.assertEqual(record['routing']['strategy_ids'], self.ids)
        self.assertEqual(record['calls'][1]['system_prompt'], adapter.role)
        history = [dict(iter=0, analysis='First analysis', injection=payload.text,
                        security=False, utility=True, final_output='Final receipt',
                        trajectory='PRIVATE_TRACE')]
        episode = types.SimpleNamespace(gold=False, error=None)
        adapter.generate_with_history(self.case, payload, episode, ('PRIVATE_EVIDENCE',), 'P1', history)
        self.assertEqual(seen[2][0], 'attacker_revise')
        self.assertEqual(seen[2][1], '\n'.join(reference['build_user_message_blocks'](
            document, self.case, history, threat_model='black_box')))
        self.assertNotIn('PRIVATE_', seen[2][1])

    def test_all_arms_share_one_route_and_new_p0_with_branch_local_full_history(self):
        seen, executions = [], []
        counter = 0
        def decide(prompt, stage):
            nonlocal counter
            seen.append((stage, prompt))
            if stage == 'router':
                return '<choice>' + ', '.join(self.ids) + '</choice>'
            counter += 1
            return f'<analysis>analysis-{counter}</analysis><injection>candidate-{counter}</injection>'
        def execute(case, payload, eid):
            executions.append((eid, payload.text))
            return TargetResult(False, final='plain final', utility=True,
                black_box_final='VISIBLE-' + eid, raw={'secret': 'PRIVATE_TRACE'})
        self.case['private_judge'] = 'PRIVATE_JUDGE'
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(types.SimpleNamespace(execute=execute),
                OriginalPIMinerAdapter(ROOT, 'fixture', 'target', decide=decide),
                FixedObserver(), build_policies(self.config['policies']),
                RunStore(Path(directory) / 'run', {}))
            metrics = runner.run([self.case])
            records = deepcopy(runner.generations)
            for generation in records:
                if generation['policy_id'] == 'shared':
                    continue
                prompt = generation['calls'][0]['prompt']
                own = generation['policy_id']
                self.assertIn('VISIBLE-' + self.case['case_id'] + '/shared/000', prompt)
                for other in self.config['policies']:
                    if other != own:
                        self.assertNotIn('VISIBLE-' + self.case['case_id'] + '/' + other + '/', prompt)
                self.assertNotIn('PRIVATE_', prompt)
                self.assertNotIn('Requested attacker observations', prompt)
                self.assertEqual([c['stage'] for c in generation['calls']], ['attacker_revise'])
            third = next(g for g in records if g['policy_id'] == 'always_revise' and g['generation_id'].endswith('/P3'))
            self.assertEqual(third['calls'][0]['prompt'].count('### Attempt '), 3)
            # U fallback retries P0 once, then the original Attacker sees BOTH outcomes.
            stage = next(g for g in records if g['policy_id'] == 'stage_aware')
            self.assertEqual(stage['calls'][0]['prompt'].count('### Attempt '), 2)
            self.assertEqual(stage['calls'][0]['prompt'].count('analysis-1'), 2)
        self.assertEqual([s for s, _ in seen].count('router'), 1)
        self.assertEqual([s for s, _ in seen].count('attacker_p0'), 1)
        self.assertEqual(executions[0][1], 'candidate-1')
        self.assertNotEqual(executions[0][1], self.case['p0'])
        self.assertEqual(len(executions), 16)
        self.assertEqual({p for eid, p in executions if '/always_retry/' in eid}, {'candidate-1'})
        self.assertEqual(metrics['physical_total_cost']['generator_calls_by_stage']['router'], 1)
        self.assertEqual(metrics['policies']['always_retry']['physical_post_failure_cost']['generator_model_calls'], 0)

    def test_invalid_route_stops_before_target_and_keeps_call_record(self):
        def execute(*args):
            self.fail('Target must not run after router failure')
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(types.SimpleNamespace(execute=execute),
                OriginalPIMinerAdapter(ROOT, 'fixture', 'target', decide=lambda *args: '<choice>invalid</choice>'),
                FixedObserver(), build_policies(self.config['policies']), RunStore(Path(directory) / 'run', {}))
            metrics = runner.run([self.case])
        self.assertEqual(metrics['physical_total_cost']['target_executions'], 0)
        self.assertEqual(metrics['physical_total_cost']['generator_calls_by_stage'], {'router': 1})
        self.assertTrue(all(o['status'] == 'GENERATION_ERROR' for o in runner.outcomes))

    def test_last_assistant_projection_matches_original_without_tool_results(self):
        messages = [dict(role='assistant', content='PRIVATE_EARLIER'),
                    dict(role='tool', content='PRIVATE_RESULT'),
                    dict(role='assistant', content='Final', tool_calls=[dict(function='send', args={'x': 1})])]
        reference = original_functions('iterative_attack_common.py',
            ['format_final_output', 'extract_final_output'], dict(vars(final_output)))
        result = final_output.extract_final_output(messages)
        self.assertEqual(result, reference['extract_final_output'](messages))
        self.assertIn('[tool_call]', result)
        self.assertNotIn('PRIVATE', result)

    def test_default_config_and_freeze_cover_entire_router_library_and_role(self):
        config = load_config(ROOT / 'experiments/e0_native_dev.yaml')
        self.assertEqual(config['attacker']['mode'], 'original')
        manifest = freeze(config)
        self.assertIn('src/vendor/attacker_role.txt', manifest['inputs_sha256'])
        for path in (ROOT / 'strategy_library').rglob('*'):
            if path.is_file():
                self.assertIn(str(path.relative_to(ROOT)), manifest['inputs_sha256'])
        manifest['inputs_sha256']['src/vendor/attacker_role.txt'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            check_frozen(manifest)
        config['attacker']['mode'] = 'invalid'
        with self.assertRaisesRegex(ValueError, 'Attacker mode'):
            validate(config)

    def test_main_selects_original_by_default_and_grounded_explicitly(self):
        from main import adapters
        from src.attacker.piminer import PIMinerAdapter
        config = load_config(ROOT / 'experiments/e0_native_dev.yaml')
        with patch('src.adapters.agentdojo.AgentDojoAdapter'):
            _, attacker = adapters(config)
            self.assertIsInstance(attacker, OriginalPIMinerAdapter)
            self.assertEqual(attacker.target_model, config['target']['model'])
            self.assertFalse(attacker.use_cached_p0)
            config['attacker']['mode'] = 'grounded'
            _, attacker = adapters(config)
            self.assertIsInstance(attacker, PIMinerAdapter)

    def test_transport_uses_original_role_and_no_tool_access(self):
        from src.attacker.transport import RoleDecision
        process = types.SimpleNamespace(returncode=0, communicate=lambda *a, **k:
            (json.dumps({'result': '<injection>x</injection>', 'usage': {'input_tokens': 5}}), ''), poll=lambda: 0)
        role = (ROOT / 'src/vendor/attacker_role.txt').read_text()
        with patch('src.attacker.transport.subprocess.Popen', return_value=process) as popen:
            transport = RoleDecision('fixture', 'max', 30, role)
            transport('unchanged prompt', 'attacker_p0')
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index('--system-prompt') + 1], role)
        self.assertEqual(command[command.index('--tools') + 1], '')
        self.assertIn('--strict-mcp-config', command)
        self.assertEqual(transport.calls[0]['usage']['input_tokens'], 5)


if __name__ == '__main__':
    unittest.main()
