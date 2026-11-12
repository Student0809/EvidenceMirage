import json
from scripts.summarize_asr_call_journal import summarize


def test_http_retry_and_cli_totals_do_not_double_count_and_unknown_is_not_zero(tmp_path):
    pricing = {'attacker': {'offpeak': {'cache_hit':.02,'cache_miss':1,'output':4},
                           'peak': {'cache_hit':.04,'cache_miss':2,'output':8}},
               'target': {'status':'unverified'}}
    common = dict(started_at='2026-11-01T12:00:00+00:00',finished_at='2026-11-01T12:00:01+00:00',
                  stage='generator',dispatched=True,status='completed',event='finish')
    success = dict(common,call_id='http1',kind='attacker_http',usage_complete=True,
                   usage={'input_tokens':1000,'output_tokens':100,'cache_read_input_tokens':10000})
    retry = dict(common,call_id='http0',kind='attacker_http',status='error',status_code=429,usage=None)
    cli = dict(common,call_id='cli',kind='attacker_cli',usage={'input_tokens':999999})
    target = dict(common,call_id='target',kind='target_http',usage={'prompt_tokens':100,'completion_tokens':10})
    path = tmp_path/'calls.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in [dict(success,event='dispatch',usage=None),retry,success,cli,target]))
    result = summarize([path],pricing)
    assert result['http_attempts']==3
    assert result['logical_attacker_invocations']==1
    assert result['groups']['attacker_http']['tokens_observed']['input_tokens']==1000
    assert abs(result['monetary']['known_subtotal_minimum']-.0016)<1e-12
    assert result['monetary']['total'] is None
    assert result['monetary']['unknown_amount_requests']==2
    assert not result['issues']


def test_interrupted_stream_partial_usage_never_becomes_complete_cost(tmp_path):
    row=dict(call_id='a',event='finish',kind='attacker_http',stage='planner',dispatched=True,
             status='error',usage_complete=False,usage={'input_tokens':10,'output_tokens':2,'cache_read_input_tokens':0})
    path=tmp_path/'calls.jsonl';path.write_text(json.dumps(row)+'\n')
    result=summarize([path],{'target':{'status':'unverified'}})
    assert result['groups']['attacker_http']['partial_stream_usage']==1
    assert result['monetary']['total'] is None
    assert result['monetary']['unknown_amount_requests']==1
