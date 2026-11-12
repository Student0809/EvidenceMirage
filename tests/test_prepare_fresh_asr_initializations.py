"""All decisions and transport are local mocks; no model or Target is called."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts import prepare_fresh_asr_initializations as launch
from scripts.summarize_asr_call_journal import attribution
from src.attacker.call_logging import begin_call, dispatch_call, finish_call, stop_on_status
from src.attacker.initialization import InitializationBundle, sha256_bytes
from src.attacker.original import OriginalPIMinerAdapter
from src.engine.config import ROOT, load_cases, load_config
from src.vendor import router


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path
    (root / 'src/vendor').mkdir(parents=True)
    (root / 'scripts').mkdir()
    (root / 'main.py').write_text('# frozen test source\n')
    (root / 'src/vendor/attacker_role.txt').write_text((ROOT / 'src/vendor/attacker_role.txt').read_text())
    strategy = Path(router._discover_strategies('agentdojo')[0]['path'])
    (root / 'strategy_library').mkdir()
    (root / 'strategy_library' / strategy.name).write_bytes(strategy.read_bytes())
    cases = []
    original = load_cases(load_config(ROOT / 'experiments/e0_smoke.yaml'))[0]
    for i in range(3):
        case = deepcopy(original)
        case.update(case_id=f'c{i:03}', step_index=0, user_task_id=f'user_task_{i}',
                    private_trajectory='PRIVATE_HISTORY_MUST_NOT_BE_SENT')
        cases.append(case)
    (root / 'cases.jsonl').write_text(''.join(json.dumps(c) + '\n' for c in cases))
    write(root / 'source_manifest.json', dict(snapshot=str(root), files={
        name: sha256_bytes((root / name).read_bytes()) for name in launch.source_files(root)}))
    write(root / 'reservation.json', dict(case_ids=[c['case_id'] for c in cases]))
    write(root / 'preparation_audit.json', dict(status='passed', api_calls=0))
    write(root / 'candidate_freeze.json', dict(candidate_frozen=True, candidate_id='fixture'))
    protocol = dict(schema_version=1, kind='fresh_shared_initialization',
                    source_snapshot=str(root), cases_path='cases.jsonl',
                    source_manifest_path='source_manifest.json', reservation_path='reservation.json',
                    preparation_audit_path='preparation_audit.json', candidate_freeze_path='candidate_freeze.json',
                    input_sha256={p: sha256_bytes((root / p).read_bytes()) for p in
                                  ('cases.jsonl', 'source_manifest.json', 'reservation.json',
                                   'preparation_audit.json', 'candidate_freeze.json')},
                    strategy_sha256={p: sha256_bytes((root / p).read_bytes()) for p in launch.strategy_files(root)},
                    candidate_frozen=True, paid_execution_authorized=True, candidate_id='fixture',
                    case_ids=[c['case_id'] for c in cases], assigned_cases=3,
                    attacker_model='mock-attacker', target_model='mock-target',
                    attacker_effort='max', timeout_seconds=1, semantics=deepcopy(launch.SEMANTICS))
    write(root / 'protocol.json', protocol)
    monkeypatch.setattr(router, 'PROJECT_ROOT', root)
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://mock.invalid')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'test-secret-never-persist')
    monkeypatch.setattr(OriginalPIMinerAdapter, 'preflight', lambda self: pytest.fail('No preflights allowed'))
    return root, protocol


class MockProxy:
    starts = 0
    def __init__(self, upstream, timeout):
        assert upstream == 'https://mock.invalid'
    def start(self):
        type(self).starts += 1
        return 'http://mock-loopback.invalid'
    def close(self):
        pass


def factory(root, fail_at=None, status_code=500, mutate_after=None):
    called = []
    class MockDecision:
        def __init__(self, model, effort, timeout, system_prompt):
            self.calls = []
            self.system_prompt = system_prompt
        def __call__(self, prompt, stage):
            assert 'PRIVATE_HISTORY_MUST_NOT_BE_SENT' not in prompt
            called.append(stage)
            record = dict(stage=stage, model='mock-attacker', status='running')
            self.calls.append(record)
            begin_call(record, 'attacker_cli')
            dispatch_call(record)
            http = dict(stage=stage, model='mock-attacker', parent_call_id=record['call_id'], status='running')
            begin_call(http, 'attacker_http')
            dispatch_call(http)
            # The generation intent is durable before any provider dispatch.
            records = list((root / 'out').glob('c*/raw/generation.json'))
            assert len(records) == 3
            assert sum(json.loads(p.read_text())['generation']['status'] == 'running' for p in records) == 1
            try:
                if len(called) == fail_at:
                    record.update(status='error', status_code=status_code)
                    http.update(status='error', status_code=status_code)
                    stop_on_status(http)
                    raise RuntimeError('mock failure')
                record.update(status='completed')
                http.update(status='completed', status_code=200, usage_complete=True,
                            usage=dict(input_tokens=10, output_tokens=3, cache_read_input_tokens=0))
                if len(called) == mutate_after:
                    (root / 'main.py').write_text('# changed after dispatch\n')
                if stage == 'router':
                    return '<choice>' + router._discover_strategies('agentdojo')[0]['id'] + '</choice>'
                return '<analysis>Mock public reasoning</analysis><injection>Mock payload</injection>'
            finally:
                finish_call(http)
                finish_call(record)
    return MockDecision, called


def run(fixture, **options):
    root, _ = fixture
    decision, called = factory(root, **options)
    result = launch.prepare(root, 'protocol.json', sha256_bytes((root / 'protocol.json').read_bytes()),
                            'out', execute=True, decision_factory=decision, proxy_factory=MockProxy)
    return result, called


def test_whole_cohort_public_replay_and_accounting(fixture):
    root, _ = fixture
    result, called = run(fixture)
    assert result['status'] == 'ready'
    assert called == ['router', 'attacker_p0'] * 3
    assert result['target_executions'] == result['target_preflight_calls'] == 0
    assert result['accounting']['actual_http_attempts'] == 6
    assert result['accounting']['cost_usd'] is None
    assert result['semantics']['share_per_arm'] == .5
    assert result['semantics']['multiply_cost_by_repeats'] is False
    cases = [json.loads(line) for line in (root / 'cases.jsonl').read_text().splitlines()]
    bundle = InitializationBundle(root, result['bundle'])
    for case in cases:
        assert bundle.for_case(case)['payload'] == 'Mock payload'
    replay = json.loads((root / 'out/public_prompt_replay.json').read_text())
    assert replay['status'] == 'passed' and replay['api_calls'] == 0
    owners, _ = attribution([root / 'out'])
    assert len(owners) == 6 and set(owners.values()) == {str(root / 'out' / c['case_id']) for c in cases}
    for path in (root / 'out').rglob('*.json*'):
        assert 'PRIVATE_HISTORY_MUST_NOT_BE_SENT' not in path.read_text()
        assert 'test-secret-never-persist' not in path.read_text()
    with pytest.raises(FileExistsError, match='no silent resume'):
        run(fixture)


def test_failed_generation_keeps_denominator_and_never_retries(fixture):
    root, _ = fixture
    result, called = run(fixture, fail_at=2)
    assert result['status'] == 'incomplete' and result['bundle'] is None
    assert called == ['router', 'attacker_p0'] * 3
    assert result['case_statuses'] == dict(c000='GENERATION_ERROR', c001='ready', c002='ready')
    assert not (root / 'out/bundle').exists()
    failed = json.loads((root / 'out/c000/raw/generation.json').read_text())
    assert failed['generation']['calls'][0]['output'].startswith('<choice>')
    assert failed['generation']['calls'][1]['status'] == 'error'
    journal = [json.loads(line) for line in (root / 'out/calls.jsonl').read_text().splitlines()]
    assert any(r['status_code'] == 500 for r in journal)


def test_http_402_stops_new_stages_and_cases(fixture):
    root, _ = fixture
    result, called = run(fixture, fail_at=1, status_code=402)
    assert called == ['router']
    assert result['status'] == 'incomplete' and result['stop_marker'] is True
    assert result['case_statuses'] == dict(c000='GENERATION_ERROR', c001='not_started', c002='not_started')
    assert (root / 'out/STOP').exists() and not (root / 'out/bundle').exists()


@pytest.mark.parametrize('field', ['paid_execution_authorized', 'candidate_frozen'])
def test_offline_default_and_execute_requires_explicit_permission(fixture, field):
    root, protocol = fixture
    protocol[field] = False
    write(root / 'protocol.json', protocol)
    digest = sha256_bytes((root / 'protocol.json').read_bytes())
    def forbidden(*args, **kwargs):
        pytest.fail('No calls permitted')
    result = launch.prepare(root, 'protocol.json', digest, 'out', decision_factory=forbidden, proxy_factory=forbidden)
    assert result['api_calls'] == 0 and result['status'] == 'offline_validated'
    assert not (root / 'out').exists()
    with pytest.raises(ValueError, match='authorization and candidate freeze'):
        launch.prepare(root, 'protocol.json', digest, 'out', execute=True,
                       decision_factory=forbidden, proxy_factory=forbidden)
    assert not (root / 'out').exists()


@pytest.mark.parametrize('path', ['cases.jsonl', 'main.py', 'candidate_freeze.json'])
def test_changed_source_or_input_rejected_before_generation(fixture, path):
    root, _ = fixture
    with (root / path).open('a') as stream:
        stream.write(' ')
    with pytest.raises(ValueError, match='changed'):
        run(fixture)
    assert not (root / 'out').exists()


def test_protocol_hash_and_denominator_pinned(fixture):
    root, protocol = fixture
    digest = sha256_bytes((root / 'protocol.json').read_bytes())
    protocol['case_ids'] = ['c000', 'c001']
    write(root / 'protocol.json', protocol)
    with pytest.raises(ValueError, match='Protocol SHA-256'):
        launch.prepare(root, 'protocol.json', digest, 'out')
    with pytest.raises(ValueError, match='Whole assigned cohort'):
        launch.prepare(root, 'protocol.json', sha256_bytes((root / 'protocol.json').read_bytes()), 'out')


def test_source_drift_after_router_stops_before_p0(fixture):
    root, _ = fixture
    decision, called = factory(root, mutate_after=1)
    with pytest.raises(ValueError, match='changed'):
        launch.prepare(root, 'protocol.json', sha256_bytes((root / 'protocol.json').read_bytes()),
                       'out', execute=True, decision_factory=decision, proxy_factory=MockProxy)
    assert called == ['router']
    result = json.loads((root / 'out/manifest.json').read_text())
    assert result['status'] == 'incomplete'
    assert result['case_statuses'] == dict(c000='GENERATION_ERROR', c001='not_started', c002='not_started')
    assert not (root / 'out/bundle').exists()


def test_unaccounted_generation_refuses_bundle(fixture, monkeypatch):
    root, _ = fixture
    monkeypatch.setattr(launch, 'verify_accounting', lambda *args: (_ for _ in ()).throw(ValueError('Missing journal')))
    with pytest.raises(ValueError, match='Missing journal'):
        run(fixture)
    assert json.loads((root / 'out/manifest.json').read_text())['status'] == 'incomplete'
    assert not (root / 'out/bundle').exists()
