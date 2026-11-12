from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.run_evidence_parallel import validate_protocol
from src.attacker.experience import RollingExperience
from src.engine.config import ROOT, load_config


def test_disabled_rolling_memory_neither_reads_nor_publishes(tmp_path):
    memory = RollingExperience(tmp_path, enabled=False)
    memory.path.write_text('PRIVATE_OLD_EXPERIENCE_INVALID_JSON')
    with patch.object(memory, '_read_entries', side_effect=AssertionError('must not read')):
        text, metadata = memory.snapshot('c004')
        assert text == '' and metadata['case_ids'] == [] and metadata['enabled'] is False
        assert memory.publish({}, [], {}, []) is False
    assert memory.path.read_text() == 'PRIVATE_OLD_EXPERIENCE_INVALID_JSON'


def test_selection_is_frozen_explicit_nonempty_and_ordered():
    config = load_config(ROOT/'experiments/a2_v2_plan_memory_test30_20261031_224926.yaml')
    config['experiment']['assigned_case_ids'] = ['c002', 'c000']
    with patch('scripts.run_evidence_parallel.check_frozen', return_value=[
            {'case_id':'c000'}, {'case_id':'c001'}, {'case_id':'c002'}]):
        cases, _ = validate_protocol({'config':config})
        assert [c['case_id'] for c in cases] == ['c002','c000']
        for bad in [[], ['c000','c000'], ['missing'], [None]]:
            config['experiment']['assigned_case_ids'] = bad
            with pytest.raises(ValueError, match='assigned_case_ids'):
                validate_protocol({'config':config})
