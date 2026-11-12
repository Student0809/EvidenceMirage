import json
from pathlib import Path

from src.feedback.ablation import AssessmentModules
from src.feedback.history import history_item
from src.mismatch.data import ROOT, digest, load_attempt
from src.mismatch.reclassify import classify_legacy_g00

SOURCE = ROOT/'runs/mismatch18_G00_continue_20261109_234253'


def test_reclassification_preserves_frozen_records_and_is_idempotent(tmp_path):
    names = ('manifest.json', 'metrics.json', 'status.json', 'scoring_receipt.json')
    before = {name: digest(SOURCE/name) for name in names}
    for name in names:
        (tmp_path/name).symlink_to(SOURCE/name)
    (tmp_path/'branches').symlink_to(SOURCE/'branches', target_is_directory=True)
    record = classify_legacy_g00(tmp_path)
    assert record['group'] == 'G10'
    assert record['original_group'] == 'G00'
    assert record['tool_evidence'] and not record['historical_experience']
    assert record['summary']['online_success_count']['5'] == 8
    assert record['frozen_artifacts_unchanged']
    assert not record['recovery_compatible']
    assert classify_legacy_g00(tmp_path) == record
    assert {name: digest(SOURCE/name) for name in names} == before
    assert (tmp_path/'RESULTS_G10.md').exists()


def test_new_g10_matches_all_18_legacy_g00_initial_assessment_prompts():
    modules = AssessmentModules('G10')
    folders = sorted((SOURCE/'branches').iterdir())
    assert len(folders) == 18
    for folder in folders:
        job = json.loads((folder/'job_public.json').read_text())
        cached = json.loads((folder/'calls/000.json').read_text())
        initial = load_attempt(job['input'])
        def model(prompt, stage):
            assert stage == 'assessment'
            assert prompt == cached['prompt'], folder.name
            return cached['output']
        modules.assess(initial, [history_item(initial)], 5, model,
                       feedback_view='authorized_evidence')
