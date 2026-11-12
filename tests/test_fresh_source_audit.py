from copy import deepcopy
import json
from pathlib import Path
import tempfile
import pytest

from tests.test_fresh_initializations import record_for_case
from scripts.export_fresh_initializations import export_records
from scripts.audit_fresh_initializations import verify_sources, verify_initial_execution
from src.engine.config import ROOT


def test_public_source_audit_replays_exact_prompt_and_rejects_rehashed_tampering():
    case, record = record_for_case()
    with tempfile.TemporaryDirectory(dir=ROOT / 'runs/asr_goal_20261101/offline') as folder:
        root = Path(folder)
        dataset = root / 'cases.jsonl'
        dataset.write_text(json.dumps(case) + '\n')
        path = export_records(ROOT, dataset, root / 'valid', [record])
        result = verify_sources(ROOT, path, [case], 'target-fixture')
        assert result['status'] == 'passed' and result['assigned_cases'] == 1
        assert result['api_calls'] == 0
        bad = deepcopy(record)
        bad['generation']['calls'][1]['prompt'] += '\nPRIVATE_FEEDBACK'
        path = export_records(ROOT, dataset, root / 'tampered', [bad])
        with pytest.raises(ValueError, match='fresh_public_prompt_mismatch'):
            verify_sources(ROOT, path, [case], 'target-fixture')
        with pytest.raises(ValueError, match='fresh_public_prompt_mismatch'):
            verify_sources(ROOT, root / 'valid/initializations.json', [case], 'wrong-target')


def test_fresh_target_provenance_checks_import_and_new_execution(tmp_path):
    case, source = record_for_case()
    generation = source['generation']
    entry = dict(case_id=case['case_id'], payload=generation['payload'],
                 payload_sha256=generation['payload_sha256'], analysis=generation['analysis'],
                 source_metadata={'kind': 'fresh_public_generation'})
    directory = tmp_path / case['case_id']
    directory.mkdir()
    record = dict(entry, calls=[], candidates=[], initialization='fresh_shared_route_and_p0',
                  reused_target_feedback=False)
    (directory / 'generation.json').write_text(json.dumps(record))
    (directory / 'target.json').write_text(json.dumps({'payload': entry['payload']}))
    start = dict(event='TARGET_STARTED', episode_id='c000/shared/000',
                 payload_sha256=entry['payload_sha256'], record_origin='new_execution')
    events = [dict(event='GENERATION_COMPLETED', generation_id='c000/shared/P0',
                   raw_path='generation.json'), start]
    def save_events():
        (directory / 'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    save_events()
    episode = dict(episode_id='c000/shared/000', raw_path='target.json', payload_id='P0',
                   phase='initial', gold=False, status='completed')
    (directory / 'episodes.jsonl').write_text(json.dumps(episode)+'\n')
    assert verify_initial_execution(tmp_path, entry)['status'] == 'passed'
    start['record_origin'] = 'historical'
    save_events()
    with pytest.raises(ValueError, match='fresh_initial_target_reused'):
        verify_initial_execution(tmp_path, entry)
