import pytest
from scripts.analyze_asr_pairs import analyze


def report(values):
    rows=[dict(case_id='c'+str(i),strict_success=hit,initial_success=hit,rescued_success=False,
               complete=True,first_success={'source':'initial'} if hit else None) for i,hit in enumerate(values)]
    return dict(assigned_cases=len(rows),cases=rows,generation_error_cases=0,complete_asr=sum(values)/len(values))


def test_cluster_repeats_are_not_counted_as_independent_cases():
    pairs=[(report([False,True]),report([True,False]))]*3
    result=analyze(pairs,['c0','c1'],bootstrap_samples=1000)
    assert result['mean_absolute_gain']==0
    assert result['cluster_bootstrap']['low']==-1
    assert result['cluster_bootstrap']['high']==1
    assert result['repeat_gain_sample_std']==0
    assert not result['goal_achieved']


def test_incomplete_or_missing_case_cannot_shrink_denominator():
    left=report([True,False]);right=report([True,False])
    right['cases'][1]['complete']=False;right['complete_asr']=None
    result=analyze([(left,right)],['c0','c1'],bootstrap_samples=20)
    assert result['rounds'][0]['candidate']['end_to_end_success_rate']==.5
    assert result['rounds'][0]['candidate']['complete_asr'] is None
    right['cases'].pop()
    with pytest.raises(ValueError,match='denominator'):analyze([(left,right)],['c0','c1'])
