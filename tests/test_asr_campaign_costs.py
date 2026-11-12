from copy import deepcopy
import pytest
from scripts.summarize_asr_campaign_costs import aggregate


def entry(name, role, amount, denominator=30):
    return dict(id=name, phase='holdout', role=role, completed=True,
                assigned_case_repeats=denominator,
                report=dict(records=[{'call_id':name}],http_attempts=1,
                            total_comparable_cny_equivalent=amount,
                            known_subtotal_cny_equivalent=amount or 0,
                            unknown_cost_requests=int(amount is None)))


def test_shared_initialization_charged_once_across_repeats_and_all_preflight_cost_retained():
    entries = [entry('shared','shared_initialization',12,0)]
    entries += [entry('r'+str(i),'reference',10) for i in range(3)]
    entries += [entry('c'+str(i),'candidate',12) for i in range(3)]
    result = aggregate(entries)
    assert result['total_cny_equivalent'] == 78
    phase = result['phases']['holdout']
    assert phase['reference']['total_cny_equivalent'] == 36
    assert phase['candidate']['total_cny_equivalent'] == 42
    assert phase['cost_ratio'] == pytest.approx(42/36)
    assert phase['reference']['assigned_case_repeats'] == 90
    assert phase['cost_ratio_1_25_met'] is True


def test_missing_incomplete_or_overlapping_costs_cannot_pass():
    rows = [entry('r','reference',10),entry('c','candidate',None)]
    assert aggregate(rows)['total_cny_equivalent'] is None
    assert aggregate(rows)['phases']['holdout']['cost_ratio_1_25_met'] is None
    rows[1]['report'] = None
    assert aggregate(rows)['complete'] is False
    rows[1] = entry('c','candidate',1)
    rows[1]['completed'] = False
    assert aggregate(rows)['total_cny_equivalent'] is None
    rows.append(deepcopy(rows[0]))
    with pytest.raises(ValueError,match='double count'):aggregate(rows)
