"""Public-feedback contract at the adapter/transport seam; no remote calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.attacker.aligned import PIMinerAlignedAdapter
from src.attacker.original import OriginalPIMinerAdapter
from src.attacker.piminer import GenerationError
from src.engine.config import ROOT, load_cases, load_config
from src.engine.storage import RunStore
from src.schema import Payload
from src.vendor.final_output import extract_final_output


STRATEGY = 'fixture-strategy'
QUOTE = 'Use only observable feedback.'
DOCUMENT = f'===== Candidate strategy 1/1: {STRATEGY} =====\n\n{QUOTE}'
OUTPUT = (f'<analysis>Observed: final self-report. Uncertain: intermediate execution. '
          f'Strategy basis: <strategy_ref id="{STRATEGY}">{QUOTE}</strategy_ref> '
          'Revision hypothesis: compare the next visible result.</analysis>'
          '<injection>synthetic candidate</injection>')


def case_fixture():
    return dict(case_id='synthetic', suite_name='synthetic', user_task_id='task',
                injection_task_id='goal', user_task_prompt='Summarize the fixture.',
                injection_task_goal='Use the approved synthetic result.',
                context_with_placeholder='{INJECTION:fixture}', injection_candidates=['fixture'],
                private_state='PRIVATE_CASE_SENTINEL')


def history_fixture(payload, analysis='', index=0):
    return dict(iter=index, analysis=analysis, injection=payload.text, security=False,
                utility=True, final_output='I completed the requested operation.',
                trajectory='PRIVATE_TRACE_SENTINEL')


class SessionFactory:
    def __init__(self):
        self.sessions = []

    def __call__(self, **options):
        session = SimpleNamespace(options=options, session_id='synthetic-session', calls=[])
        class Capture:
            session_id = session.session_id
            calls = session.calls
            def __call__(self, prompt, stage):
                self.calls.append(dict(prompt=prompt, stage=stage))
                return OUTPUT
        capture = Capture()
        capture.options = options
        self.sessions.append(capture)
        return capture


class RevisionProfileTests(unittest.TestCase):
    def original(self, profile='calibrated', decide=None):
        adapter = OriginalPIMinerAdapter(ROOT, 'capture', 'capture',
                                         decide=decide, revision_profile=profile)
        adapter.routes['synthetic'] = dict(strategy_ids=[STRATEGY], strategy_paths=[],
                                          strategy_sha256={}, document=DOCUMENT)
        return adapter

    def test_real_transport_uses_original_role_for_p0_and_calibrated_role_for_revision(self):
        process = SimpleNamespace(returncode=0, poll=lambda: 0,
            communicate=lambda *a, **k: (json.dumps({'result': OUTPUT}), ''))
        adapter = self.original()
        case = case_fixture()
        with patch('src.attacker.transport.subprocess.Popen', return_value=process) as popen:
            p0, initial = adapter.generate(case, None, None, (), 'P0')
            _, revised = adapter.generate_with_history(case, p0,
                SimpleNamespace(gold=False, error=None), ('PRIVATE_EVIDENCE_SENTINEL',),
                'P1', [history_fixture(p0, initial['analysis'])])
        commands = [call.args[0] for call in popen.call_args_list]
        original_role = (ROOT/'src/vendor/attacker_role.txt').read_text()
        roles = [c[c.index('--system-prompt')+1] for c in commands]
        self.assertEqual(roles[0], original_role)
        self.assertNotEqual(roles[1], original_role)
        self.assertNotIn('full trajectory for each attempt', roles[1])
        self.assertIn('security=False', roles[1])
        self.assertIn('self-report', roles[1])
        self.assertIn('Absence of a tool call', roles[1])
        for command in commands:
            self.assertEqual(command[command.index('--tools')+1], '')
        prompt = revised['calls'][0]['prompt']
        self.assertNotIn('PRIVATE_', prompt)
        self.assertEqual(revised['revision_profile'], 'calibrated')
        self.assertEqual(initial['revision_profile'], 'original')
        self.assertTrue(revised['revision_audit']['strategy_references'][0]['source_verified'])

    def test_original_profile_retains_exact_prompt_and_role(self):
        captured = []
        decide = lambda prompt, stage: captured.append(prompt) or OUTPUT
        calibrated = self.original('calibrated', decide)
        legacy = self.original('original', decide)
        plain = OriginalPIMinerAdapter(ROOT, 'capture', 'capture', decide=decide)
        plain.routes = deepcopy(legacy.routes)
        payload = Payload('P0', 'synthetic initial')
        records = []
        for adapter in [calibrated, legacy, plain]:
            _, record = adapter.generate_with_history(case_fixture(), payload,
                SimpleNamespace(gold=False, error=None), (), 'P1', [history_fixture(payload)])
            records.append(record)
        self.assertEqual(captured[1], captured[2])
        self.assertEqual(records[1]['calls'][0]['system_prompt'], records[2]['calls'][0]['system_prompt'])
        self.assertNotEqual(captured[0], captured[1])

    def test_citations_must_match_the_named_strategy_not_another_candidate(self):
        from src.attacker.revision import audit_revision
        document = DOCUMENT + '\n===== Candidate strategy 2/2: other =====\nDifferent source text.'
        output = ('<strategy_ref id="fixture-strategy">Different source text.</strategy_ref>'
                  '<strategy_ref id="unknown">Use only observable feedback.</strategy_ref>')
        audit = audit_revision(output, document, [STRATEGY, 'other'])
        self.assertEqual([r['source_verified'] for r in audit['strategy_references']], [False, False])
        self.assertEqual(audit['enforcement'], 'advisory')
        self.assertFalse(audit['interpretation_verified'])

    def test_missing_or_overlong_reference_never_claims_strategy_use(self):
        from src.attacker.revision import audit_revision
        missing = audit_revision('No supplied strategy is applicable.', DOCUMENT, [STRATEGY])
        self.assertEqual(missing['strategy_references'], [])
        self.assertFalse(missing['has_verified_reference'])
        self.assertFalse(missing['interpretation_verified'])
        quote = 'x' * 241
        document = f'===== Candidate strategy 1/1: {STRATEGY} =====\n{quote}'
        audit = audit_revision(f'<strategy_ref id="{STRATEGY}">{quote}</strategy_ref>', document, [STRATEGY])
        self.assertFalse(audit['has_verified_reference'])

    def test_unknown_profile_is_rejected_before_any_call(self):
        with self.assertRaisesRegex(ValueError, 'revision_profile'):
            self.original('unknown', lambda *args: self.fail('Unexpected model call'))

    def test_aligned_calibrated_session_full_delta_and_optional_a2_view(self):
        cases = load_cases(load_config(ROOT/'experiments/evidence_piminer_test_asr5_20261030.yaml'))
        case = next(c for c in cases if c['case_id'] == 'c000')
        factory = SessionFactory()
        adapter = PIMinerAlignedAdapter(ROOT, 'capture', 'capture',
            'data/piminer_aligned_20261030/initializations.json', session_factory=factory,
            revision_profile='calibrated', a2_library='strategy_library_a2/library.json',
            a2_retrieval='data/a2/dev.retrieval.json')
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory)/'run', {})
            adapter.bind_run(store.path, ['test'])
            p0, initial = adapter.generate(case, None, None, (), 'P0')
            self.assertFalse(factory.sessions)
            adapter.begin_branch(case, 'test')
            history = [history_fixture(p0, initial['analysis'])]
            p1, first = adapter.generate_with_history(case, p0,
                SimpleNamespace(gold=False, error=None), (), 'P1', history)
            history.append(history_fixture(p1, first['analysis'], 1))
            _, second = adapter.generate_with_history(case, p1,
                SimpleNamespace(gold=False, error=None), (), 'P2', history)
        self.assertEqual(len(factory.sessions), 1)
        role = factory.sessions[0].options['system_prompt']
        self.assertNotIn('full trajectory for each attempt', role)
        self.assertEqual(first['prompt_mode'], 'full')
        self.assertEqual(second['prompt_mode'], 'delta')
        self.assertIn('a2', first['routing'])
        self.assertIn('Selected original examples', first['calls'][0]['prompt'])
        self.assertLess(first['routing']['a2']['a2_document_chars'], first['routing']['a2']['original_document_chars'])
        self.assertNotIn('re-run `next`', second['calls'][0]['prompt'])
        self.assertNotIn('first `next` output', second['calls'][0]['prompt'])
        self.assertEqual(first['routing']['strategy_ids'], initial['routing']['strategy_ids'])

    def test_aligned_a2_options_must_be_supplied_together(self):
        with self.assertRaisesRegex(ValueError, 'a2_library.*a2_retrieval'):
            PIMinerAlignedAdapter(ROOT, 'capture', 'capture',
                'data/piminer_aligned_20261030/initializations.json',
                a2_library='strategy_library_a2/library.json')

    def test_aligned_a2_rejects_different_source_before_caching_route(self):
        cases = load_cases(load_config(ROOT/'experiments/evidence_piminer_test_asr5_20261030.yaml'))
        case = next(c for c in cases if c['case_id'] == 'c000')
        adapter = PIMinerAlignedAdapter(ROOT, 'capture', 'capture',
            'data/piminer_aligned_20261030/initializations.json',
            a2_library='strategy_library_a2/library.json', a2_retrieval='data/a2/dev.retrieval.json')
        initial = adapter.bundle.for_case(case)
        first = initial['strategy_ids'][0]
        other = next(s for s in adapter.resources.strategies if s != first)
        adapter.resources.strategies[first]['source'] = adapter.resources.strategies[other]['source']
        for _ in range(2):
            with self.assertRaises(GenerationError) as caught:
                adapter.generate(case, None, None, (), 'P0')
            self.assertIsInstance(caught.exception.__cause__, ValueError)
            self.assertIn('strategy source differs', str(caught.exception.__cause__))
            self.assertNotIn(case['case_id'], adapter.routes)

    def test_final_output_projector_keeps_intermediate_messages_private(self):
        final = extract_final_output([
            dict(role='assistant', content='', tool_calls=[dict(function='PRIVATE_TOOL', args={})]),
            dict(role='tool', content='PRIVATE_RESULT'),
            dict(role='assistant', content='I completed the requested operation.')])
        self.assertEqual(final, 'I completed the requested operation.')


if __name__ == '__main__':
    unittest.main()
