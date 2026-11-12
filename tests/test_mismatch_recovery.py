import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from src.adapters.backoff import rate_limit_retry
from src.mismatch.data import ROOT, write_json
from src.mismatch.recovery import inspect_run, recover
from src.mismatch.online import run_branch


def test_backoff_bounds_and_no_retry_of_validation_errors():
    class Limited(Exception):
        status_code = 429
    calls = []
    def call():
        calls.append(1)
        if len(calls) < 4:
            raise Limited()
        return 'ok'
    with patch.dict('os.environ', {'PIMINER_RATE_LIMIT_RETRIES':'3',
                                  'PIMINER_RATE_LIMIT_BASE':'2','PIMINER_RATE_LIMIT_CAP':'5'}), \
         patch('src.adapters.backoff.random.uniform', side_effect=lambda a,b:b), \
         patch('src.adapters.backoff.time.sleep') as sleep:
        assert rate_limit_retry(call) == 'ok'
        assert [x.args[0] for x in sleep.call_args_list] == [2,4,5]
        with pytest.raises(ValueError):
            rate_limit_retry(lambda: (_ for _ in ()).throw(ValueError('schema')))
        assert sleep.call_count == 3
        with pytest.raises(Limited):
            rate_limit_retry(lambda: (_ for _ in ()).throw(Limited()))
        assert sleep.call_count == 6


def test_legacy_protocol_rejected_before_recovery_or_model():
    source = ROOT/'runs/mismatch18_G00_20261109_191756'
    with pytest.raises(ValueError, match='Legacy ablation protocol'):
        inspect_run(source, True)
    with patch('src.mismatch.online.configure', side_effect=AssertionError('must not configure')):
        with pytest.raises(ValueError, match='Legacy or missing ablation protocol'):
            run_branch(source/'branches/b001')


def make_frozen_source(root):
    from src.feedback.ablation import ABLATION_PROTOCOL, AssessmentModules
    from src.mismatch.data import jsonl
    from src.schema import Payload
    source = root/'source'
    folder = source/'branches/b000'
    config = AssessmentModules('G00').metadata()
    job = dict(input=jsonl(ROOT/'data/mismatch_autonomous_20261104/model_inputs.jsonl')[0],
               model='fixture', effort='max', timeout=1, view='final_only', target_config={},
               ablation='G00', ablation_protocol=ABLATION_PROTOCOL)
    write_json(folder/'job_public.json', job)
    class Transport:
        calls = []
        def __call__(self, prompt, stage):
            return json.dumps(dict(goal_status='unknown', action='Retry', action_reason='test'))
    def target(command, **kwargs):
        request = Path(command[-1])
        value = json.loads(request.read_text())
        write_json(request.parent/'feedback_public.json', dict(final_output='', packets=[],
                   error='RateLimitError', payload_sha256=Payload(**value['payload']).sha256))
    with patch('src.mismatch.online.configure'), \
         patch('src.attacker.piminer.ClaudeDecision', return_value=Transport()), \
         patch('src.mismatch.online.subprocess.run', side_effect=target):
        row = run_branch(folder)
    assert row['error_type'] == 'RateLimitError'
    write_json(source/'status.json', dict(status='completed_with_errors', results=[row], total=1))
    write_json(source/'manifest.json', dict(ablation=config))
    return source


def test_v3_frozen_prefix_reaches_recovery_boundary_without_network(tmp_path):
    source = make_frozen_source(tmp_path)
    _, _, plans = inspect_run(source, True)
    assert len(plans) == 1
    plan = plans[0]
    folder = tmp_path/'recovery/b000'
    job = json.loads((source/'branches/b000/job_public.json').read_text())
    job['recovery'] = dict(protocol='controller_boundaries_v2', calls=plan['calls'],
                           revisions=plan['revisions'], executions=plan['executions'])
    write_json(folder/'job_public.json', job)
    class Transport:
        calls = []
        def __call__(self, *args):
            raise AssertionError('assessment should replay')
    with patch('src.mismatch.online.configure'), \
         patch('src.attacker.piminer.ClaudeDecision', return_value=Transport()), \
         patch('src.mismatch.online.subprocess.run', side_effect=RuntimeError('new target boundary')):
        result = run_branch(folder)
    assert result['status'] == 'target_error'
    assert result['error_type'] == 'RuntimeError'
    call = json.loads((folder/'calls/000.json').read_text())
    assert call['replayed'] and call['status'] == 'completed'


def test_recovery_dry_run_does_not_create_output(tmp_path):
    source = make_frozen_source(tmp_path)
    output = tmp_path/'new'
    result = recover(SimpleNamespace(source=source, output=output, workers=2,
                     include_generation_errors=False, dry_run=True))
    assert len(result['branches']) == 1
    assert result['api_calls'] == 0
    assert not output.exists()


def test_recovery_rejects_cross_group_configuration(tmp_path):
    source = make_frozen_source(tmp_path)
    manifest = json.loads((source/'manifest.json').read_text())
    from src.feedback.ablation import AssessmentModules
    manifest['ablation'] = AssessmentModules('G10').metadata()
    write_json(source/'manifest.json', manifest)
    with pytest.raises(ValueError, match='configuration mismatch'):
        inspect_run(source, True)
