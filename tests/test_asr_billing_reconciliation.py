from copy import deepcopy
from datetime import datetime
import json
from scripts.reconcile_asr_billing import reconcile


def inputs(tmp_path):
    call=dict(call_id='target1',kind='target_http',model='gpt-5-nano',event='finish',status='completed',
        started_at='2026-11-01T12:00:00+00:00',finished_at='2026-11-01T12:00:20+00:00',
        usage={'prompt_tokens':100,'completion_tokens':200},attribution='c000')
    row=dict(created_at=int(datetime.fromisoformat(call['finished_at']).timestamp()),type=2,
        model_name='gpt-5-nano',quota=43,prompt_tokens=100,completion_tokens=200,group='default')
    snapshot=dict(status_code=200,success=True,rows=[row]);paths=[]
    for name in ('first','later'):
        path=tmp_path/(name+'.json');path.write_text(json.dumps(snapshot));paths.append(path)
    summary=dict(records=[call],issues=[],input_sha256={})
    pricing=dict(target={'quota_per_unit':500000},common_display_conversion={'rate':7})
    return summary,paths,pricing


def test_overlapping_poll_snapshots_are_not_double_billed(tmp_path):
    summary,paths,pricing=inputs(tmp_path)
    result=reconcile(summary,paths,pricing)
    assert result['matched_target_requests']==1 and result['actual_target_quota_known']==43
    assert result['unmatched_snapshot_receipts']==0
    assert result['total_comparable_cny_equivalent']==43/500000*7


def test_ambiguous_or_unfinished_call_does_not_claim_a_receipt(tmp_path):
    summary,paths,pricing=inputs(tmp_path)
    summary['records'].append(dict(deepcopy(summary['records'][0]),call_id='target2'))
    result=reconcile(summary,paths,pricing)
    assert result['matched_target_requests']==0 and result['unknown_cost_requests']==2
    assert result['total_comparable_cny_equivalent'] is None
    summary['records'].pop();summary['records'][0]['event']='dispatch'
    assert reconcile(summary,paths,pricing)['matched_target_requests']==0
