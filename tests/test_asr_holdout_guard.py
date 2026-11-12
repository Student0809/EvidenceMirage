"""Offline witnesses for one parent claim and bounded paired test execution."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import launch_asr_interleaved as launcher
from scripts import asr_holdout_guard as guard
from scripts import asr_interleaved_arm as arm_runner
from scripts import run_evidence_parallel as parallel


@pytest.fixture
def cohort(tmp_path, monkeypatch):
    for key in guard.CLAIM_ENV:
        monkeypatch.delenv(key, raising=False)
    cases = [{'case_id': 'c001'}, {'case_id': 'c002'}]
    arms = []
    for name in ('reference', 'candidate'):
        root = tmp_path / name
        root.mkdir()
        dataset = root / 'cases.jsonl'
        dataset.write_text(''.join(json.dumps(c) + '\n' for c in cases))
        audit = root / 'audit.json'
        audit.write_text(json.dumps(dict(status='complete', reviewer='offline-reviewer',
            selection_rule='All eligible local bases', dataset_sha256=guard.digest(dataset),
            independent_heldout=True, audit_scope='local provenance only',
            independence_qualification='Unrecorded external history is not certified',
            selected_case_ids=[c['case_id'] for c in cases])))
        source = root / 'source_manifest.json'
        source.write_text(json.dumps(dict(snapshot=str(root), files={})))
        lock = root / 'condition.lock.json'
        config = dict(split='test', backend='agentdojo', dataset=dataset.name, heldout_audit=audit.name,
            require_observable_trigger=True, policies={'stage_aware': {}},
            attacker=dict(mode='original', rolling_experience=False), post_failure_target_budget=4,
            target=dict(benchmark_version='offline-mock'),
            experiment=dict(source_snapshot_manifest=source.name))
        lock.write_text(json.dumps(dict(config=config,
            inputs_sha256={dataset.name: guard.digest(dataset), audit.name: guard.digest(audit)})))
        arms.append(dict(name=name, source_root=str(root), frozen=str(lock),
            frozen_sha256=guard.digest(lock), source_manifest_sha256=guard.digest(source)))
    protocol = dict(output=str(tmp_path / 'run'), python=sys.executable, arms=arms,
        case_ids=[c['case_id'] for c in cases], repeats=2, schedule_seed=4,
        schedule=launcher.schedule([c['case_id'] for c in cases], ['reference', 'candidate'], 2, 4),
        paid_execution_authorized=True)
    path = tmp_path / 'protocol.json'
    path.write_text(json.dumps(protocol, separators=(',', ':')) + '\n')
    return SimpleNamespace(protocol=protocol, path=path, registry=tmp_path / 'active', cases=cases)


def claimed(cohort):
    output = Path(cohort.protocol['output'])
    output.mkdir()
    output.joinpath('protocol.json').write_bytes(cohort.path.read_bytes())
    spec = guard.cohort_spec(cohort.protocol)
    path = guard.claim_cohort(cohort.registry, cohort.protocol, cohort.path.read_bytes(), spec)
    return spec, path


def child(cohort, path, name='reference', repeat='0'):
    arm = next(a for a in cohort.protocol['arms'] if a['name'] == 'reference')
    env = dict(zip(guard.CLAIM_ENV, (str(path), name, repeat)))
    output = Path(cohort.protocol['output']) / ('repeat' + repeat) / name
    manifest = json.loads(Path(arm['frozen']).read_text())
    return arm, env, output, manifest


def test_one_durable_cohort_claim_covers_all_frozen_arms_and_repeats(cohort):
    spec, path = claimed(cohort)
    for arm in cohort.protocol['arms']:
        for repeat in range(cohort.protocol['repeats']):
            env = dict(zip(guard.CLAIM_ENV, (str(path), arm['name'], str(repeat))))
            result = guard.verify_child_claim(arm['source_root'], arm['frozen'],
                json.loads(Path(arm['frozen']).read_text()), cohort.protocol['case_ids'],
                Path(cohort.protocol['output']) / f'repeat{repeat}' / arm['name'], env)
            assert result['cohort_repeat'] == repeat
            assert result['independence_qualification'] == 'Unrecorded external history is not certified'
            assert guard.evaluation_metadata({'split': 'test'}, result)['independent_heldout'] is True
    assert list(path.parent.iterdir()) == [path]
    with pytest.raises(FileExistsError):
        guard.claim_cohort(cohort.registry, cohort.protocol, cohort.path.read_bytes(), spec)
    with pytest.raises(ValueError, match='already been claimed'):
        guard.ensure_unclaimed(cohort.registry, spec)


def test_existing_main_claim_also_prevents_paired_reuse(cohort):
    spec = guard.cohort_spec(cohort.protocol)
    path = guard.claim_path(cohort.registry, spec['dataset_sha256'])
    path.parent.mkdir(parents=True)
    path.write_text('{"output":"a-previous-single-test-run","created_at":"earlier"}')
    with pytest.raises(ValueError, match='already been claimed'):
        guard.ensure_unclaimed(cohort.registry, spec)


@pytest.mark.parametrize('name,repeat', [('foreign', '0'), ('reference', '2'),
                                        ('reference', '-1'), ('reference', '0.5')])
def test_child_rejects_out_of_cohort_arm_or_repeat(cohort, name, repeat):
    _, path = claimed(cohort)
    arm, env, output, manifest = child(cohort, path, name, repeat)
    with pytest.raises(ValueError, match='outside the claimed cohort'):
        guard.verify_child_claim(arm['source_root'], arm['frozen'], manifest,
            cohort.protocol['case_ids'], output, env)


@pytest.mark.parametrize('changed', ['dataset', 'protocol', 'frozen', 'source', 'claim_dataset'])
def test_child_rejects_changed_hash_bindings(cohort, changed):
    _, path = claimed(cohort)
    arm, env, output, manifest = child(cohort, path)
    if changed == 'claim_dataset':
        value = json.loads(path.read_text()); value['dataset_sha256'] = '0' * 64
        path.write_text(json.dumps(value))
    else:
        files = dict(dataset=Path(arm['source_root']) / 'cases.jsonl',
            protocol=Path(cohort.protocol['output']) / 'protocol.json', frozen=Path(arm['frozen']),
            source=Path(arm['source_root']) / 'source_manifest.json')
        with files[changed].open('a') as stream:
            stream.write(' ')
    with pytest.raises(ValueError, match='hash|population'):
        guard.verify_child_claim(arm['source_root'], arm['frozen'], manifest,
            cohort.protocol['case_ids'], output, env)


def test_child_rejects_wrong_output_and_assigned_population(cohort):
    _, path = claimed(cohort)
    arm, env, output, manifest = child(cohort, path)
    with pytest.raises(ValueError, match='output is outside'):
        guard.verify_child_claim(arm['source_root'], arm['frozen'], manifest,
            cohort.protocol['case_ids'], output.parent / 'renamed', env)
    with pytest.raises(ValueError, match='population differs'):
        guard.verify_child_claim(arm['source_root'], arm['frozen'], manifest,
            cohort.protocol['case_ids'][:1], output, env)


@pytest.mark.parametrize('selection', [['c001'], ['c002', 'c001']])
def test_parent_rejects_subsetting_or_reordering_complete_heldout_cohort(cohort, selection):
    cohort.protocol['case_ids'] = selection
    with pytest.raises(ValueError, match='complete ordered dataset/audit'):
        guard.cohort_spec(cohort.protocol)
    assert not cohort.registry.exists()


def test_audit_cannot_silently_shrink_the_dataset(cohort):
    arm = cohort.protocol['arms'][0]
    manifest = json.loads(Path(arm['frozen']).read_text())
    audit_path = Path(arm['source_root']) / 'audit.json'
    audit = json.loads(audit_path.read_text()); audit['selected_case_ids'] = ['c001']
    audit_path.write_text(json.dumps(audit))
    manifest['inputs_sha256']['audit.json'] = guard.digest(audit_path)
    with pytest.raises(ValueError, match='complete ordered dataset/audit'):
        guard.heldout_evidence(arm['source_root'], manifest, ['c001'])


def test_split_alone_never_establishes_independent_heldout(cohort):
    arm = cohort.protocol['arms'][0]
    manifest = json.loads(Path(arm['frozen']).read_text())
    audit_path = Path(arm['source_root']) / 'audit.json'
    audit = json.loads(audit_path.read_text()); audit.pop('independent_heldout')
    audit_path.write_text(json.dumps(audit))
    manifest['inputs_sha256']['audit.json'] = guard.digest(audit_path)
    with pytest.raises(ValueError, match='independence requires'):
        guard.heldout_evidence(arm['source_root'], manifest, cohort.protocol['case_ids'])
    with pytest.raises(ValueError, match='verified cohort claim'):
        guard.evaluation_metadata({'split': 'test'})
    assert guard.evaluation_metadata({'split': 'dev'}) == dict(
        independent_heldout=False, evaluation_scope='development')


def mock_parent(cohort, monkeypatch):
    launched = []
    class Process:
        def __init__(self, command, **kwargs):
            arm = next(a for a in cohort.protocol['arms'] if a['source_root'] == str(kwargs['cwd']))
            # This stands in for the child preflight: the claim must already exist.
            guard.verify_child_claim(kwargs['cwd'], arm['frozen'],
                json.loads(Path(arm['frozen']).read_text()), cohort.protocol['case_ids'],
                command[-1], kwargs['env'])
            launched.append(kwargs['env'])
            self.queue = [{'status': 'ready'}]
            self.stdin = self; self.stdout = self; self.code = None
        def write(self, line):
            command = json.loads(line)
            if command['action'] == 'finish':
                self.queue.append({'status': 'completed'}); self.code = 0
            else:
                self.queue.append(dict(case_id=command['case_id'], status='completed'))
        def flush(self): pass
        def close(self): self.code = 1
        def readline(self): return json.dumps(self.queue.pop(0)) + '\n'
        def poll(self): return self.code
        def wait(self): return self.code
    monkeypatch.setattr(launcher, 'ROOT', cohort.registry)
    monkeypatch.setattr(launcher.subprocess, 'Popen', Process)
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0,
        stdout=json.dumps(dict(status='offline_valid', case_ids=cohort.protocol['case_ids']))))
    monkeypatch.setattr(launcher, 'configure_credentials', lambda: None)
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'offline-test-placeholder')
    return launched


def test_parent_claim_precedes_every_preflight_and_rejects_renamed_rerun(cohort, monkeypatch):
    launched = mock_parent(cohort, monkeypatch)
    monkeypatch.setattr(sys, 'argv', ['launch', '--protocol', str(cohort.path)])
    launcher.main()
    assert len(launched) == 4
    assert len({env['PIMINER_HELDOUT_CLAIM'] for env in launched}) == 1
    assert (Path(cohort.protocol['output']) / 'protocol.json').read_bytes() == cohort.path.read_bytes()
    cohort.protocol['output'] = str(Path(cohort.protocol['output']).with_name('renamed'))
    cohort.path.write_text(json.dumps(cohort.protocol))
    with pytest.raises(ValueError, match='already been claimed'):
        launcher.main()
    assert len(launched) == 4
    assert not Path(cohort.protocol['output']).exists()


def test_test_dry_run_is_read_only_and_unauthorized_protocol_cannot_claim(cohort, monkeypatch):
    launched = mock_parent(cohort, monkeypatch)
    monkeypatch.setattr(sys, 'argv', ['launch', '--protocol', str(cohort.path), '--dry-run'])
    launcher.main()
    assert not launched and not cohort.registry.exists() and not Path(cohort.protocol['output']).exists()
    cohort.protocol['paid_execution_authorized'] = False
    cohort.path.write_text(json.dumps(cohort.protocol))
    monkeypatch.setattr(sys, 'argv', ['launch', '--protocol', str(cohort.path)])
    with pytest.raises(ValueError, match='authorization'):
        launcher.main()
    assert not launched and not cohort.registry.exists() and not Path(cohort.protocol['output']).exists()


def test_interruption_after_claim_never_releases_the_cohort(cohort, monkeypatch):
    mock_parent(cohort, monkeypatch)
    starts = []
    def fail_before_preflight(command, **kwargs):
        path = Path(kwargs['env']['PIMINER_HELDOUT_CLAIM'])
        assert path.is_file()
        starts.append(path)
        raise RuntimeError('Offline injected child startup failure')
    monkeypatch.setattr(launcher.subprocess, 'Popen', fail_before_preflight)
    monkeypatch.setattr(sys, 'argv', ['launch', '--protocol', str(cohort.path)])
    with pytest.raises(RuntimeError, match='injected'):
        launcher.main()
    assert starts[0].is_file()
    assert json.loads((Path(cohort.protocol['output']) / 'status.json').read_text())['status'] == 'interrupted'
    cohort.protocol['output'] = str(Path(cohort.protocol['output']).with_name('retry'))
    cohort.path.write_text(json.dumps(cohort.protocol))
    with pytest.raises(ValueError, match='already been claimed'):
        launcher.main()
    assert len(starts) == 1 and not Path(cohort.protocol['output']).exists()


def test_test_validation_keeps_source_and_provenance_checks(cohort, monkeypatch):
    arm = cohort.protocol['arms'][0]
    monkeypatch.setattr(parallel, 'ROOT', Path(arm['source_root']))
    monkeypatch.setattr(parallel, 'check_frozen', lambda manifest: cohort.cases)
    manifest = json.loads(Path(arm['frozen']).read_text())
    assert parallel.validate_protocol(manifest)[0] == cohort.cases
    manifest['config']['require_observable_trigger'] = False
    with pytest.raises(ValueError, match='observable'):
        parallel.validate_protocol(manifest)


def test_test_arm_and_case_refuse_missing_claim_before_credentials_or_adapters(cohort, monkeypatch):
    arm = cohort.protocol['arms'][0]
    monkeypatch.setattr(arm_runner, 'ROOT', Path(arm['source_root']))
    monkeypatch.setattr(arm_runner, 'validate_protocol', lambda manifest: (cohort.cases, None))
    def forbidden():
        raise AssertionError('Credential/preflight path reached before claim verification')
    monkeypatch.setattr(arm_runner, 'configure_credentials', forbidden)
    from src.adapters import agentdojo
    monkeypatch.setattr(agentdojo, 'validate_native_cases', lambda *a: {'offline': True})
    monkeypatch.setattr(sys, 'argv', ['arm', '--frozen', arm['frozen'], '--output', cohort.protocol['output']])
    with pytest.raises(ValueError, match='parent cohort claim'):
        arm_runner.main()
    monkeypatch.setattr(parallel, 'ROOT', Path(arm['source_root']))
    monkeypatch.setattr(parallel, 'validate_protocol', lambda manifest, reference=None: (cohort.cases, None))
    with pytest.raises(ValueError, match='parent cohort claim'):
        parallel.run_case(arm['frozen'], 'c001', str(Path(cohort.protocol['output']) / 'c001'))
    with pytest.raises(ValueError, match='one-time cohort claim'):
        parallel.main()
    assert not Path(cohort.protocol['output']).exists()
