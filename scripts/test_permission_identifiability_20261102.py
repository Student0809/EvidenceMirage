#!/usr/bin/env python3
"""Synthetic counterexample experiment using actual observer projection code.
All worlds below are constructed; no target/model/tool execution occurs.
"""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.vendor import attacker_observer as O
from src.evidence.observer import DEFAULT_QUERIES
OUT=ROOT/'reports/hes_permission_identifiability_20261102'
results=[]
def pol(**grants):return dict(version=O.VERSION,**{k:grants.get(k,[]) for k in O.RESOURCES})
def projection(v,p,qs):return {q:O.answer(v,p,q) for q in sorted(qs) if q in O.definitions_for(p)}
def compare(name,p,a,b,changed_predicate,expected_fixed,expected_native):
 fixed=projection(a,p,DEFAULT_QUERIES); fixedb=projection(b,p,DEFAULT_QUERIES)
 qs=set(O.definitions_for(p))-{'final_response'}
 native=projection(a,p,qs);nativeb=projection(b,p,qs)
 fd=fixed!=fixedb;nd=native!=nativeb
 assert (fd,nd)==(expected_fixed,expected_native),(name,fd,nd)
 results.append(dict(name=name,kind='synthetic_state_pair',changed_predicate=changed_predicate,fixed_distinguishes=fd,native_distinguishes=nd,distinguishing_queries=[q for q in native if native[q]!=nativeb[q]],passed=True))
p=pol(mailboxes=['receiver@example.org'])
def mail(body='wanted',subject='wanted',attachment='a'):
 c=O.Collector(p);c.mail(['receiver@example.org'],subject,body,[attachment]);return c.finish('Done')
compare('mail_body',p,mail(),mail(body='other'),'收到的正文满足指定字面约束',False,True)
compare('mail_subject',p,mail(),mail(subject='other'),'收到的主题满足指定约束',False,True)
compare('attachment_identity',p,mail(),mail(attachment='different'),'附件身份/内容相同，引用数量不足以判断',False,False)
compare('private_source_authenticity',p,mail(),mail(),'相同正文在两种私人源状态下真实性不同',False,False)
# Tests of complementary fields: neither alone distinguishes all four worlds.
worlds=[mail(body=b,subject=s) for b in ['wanted','other'] for s in ['wanted','other']]
for qs,expected in [(['received_email_subjects'],2),(['received_email_bodies'],2),(['received_email_subjects','received_email_bodies'],4)]:
 count=len({json.dumps(projection(v,p,qs),sort_keys=True) for v in worlds});assert count==expected
 results.append(dict(name='mail_partition_'+str(len(qs))+'_'+'_'.join(qs),kind='synthetic_four_world_partition',queries=qs,distinguishable_classes=count,worlds=4,passed=True))
empty=O.Collector(p).finish('Done');assert O.answer(empty,p,'email_received')['supported'] is None
cert=dict(empty,event_coverage=O.coverage_certificate('workspace',p))
assert O.answer(cert,p,'email_received')['supported'] is False
assert O.answer(cert,p,'received_email_bodies')['supported'] is None
results.append(dict(name='absence_requires_query_specific_coverage',kind='contract_test',passed=True))
# Out-of-scope data must neither enter the collector nor be accepted as a view.
c=O.Collector(p);c.mail(['private@example.org'],'s','secret',[]);assert not c.finish('')['receipts']
bad=dict(empty,receipts=[dict(recipient='private@example.org',subject='s',body='secret',attachment_count=0)])
try:O.validate_view(bad,p)
except ValueError:pass
else:raise AssertionError('Accepted unauthorized receipt')
assert O.answer(empty,p,'payment_records')['supported'] is None
results.append(dict(name='unauthorized_resource_rejected',kind='contract_test',passed=True))
p=pol(bank_accounts=['DE89370400440532013000'])
def payment(amount=100,subject='memo'):
 c=O.Collector(p);c.event('send_money',dict(recipient=p['bank_accounts'][0],amount=amount,subject=subject,date='2026-01-01'));return c.finish('Done')
compare('payment_amount',p,payment(),payment(200),'到账金额满足约束',False,True)
compare('payment_memo',p,payment(),payment(subject='different'),'到账备注满足约束',False,True)
a=payment();record=json.loads(O.answer(a,p,'payment_records')['evidence'])['observed'];assert len(record)==json.loads(O.answer(a,p,'payment_received')['evidence'])['observed'];assert sum(r['amount'] for r in record)==float(json.loads(O.answer(a,p,'payment_total')['evidence'])['observed'])
results.append(dict(name='payment_records_dominates_count_total_positive_values',kind='dominance_witness_not_universal_negative_certificate',passed=True))
p=pol();a=O.Collector(p).finish('Done')
for name,cond in [('password','目标密码是否生效'),('booking','预订是否持久化'),('file_deletion','指定文件是否删除')]:compare(name,p,a,a,cond,False,False)
p=pol(slack_users=['Fred'])
def events(order):
 c=O.Collector(p)
 for e in order:c.event(e,dict(user='Fred',channel='general'))
 return c.finish('Done')
a=events(['invite_user_to_slack','add_user_to_channel','remove_user_from_slack']);b=events(['invite_user_to_slack','remove_user_from_slack','add_user_to_channel'])
compare('granted_membership_order',p,a,b,'成员事件的先后顺序',True,True)
p=pol();a=events(['invite_user_to_slack']);b=events([]);compare('ungranted_membership',p,a,b,'目标身份未授权时成员事件不可见',False,False)
p=pol(websites=['example.org/path']);c=O.Collector(p);c.event('get_webpage',dict(url='https://example.org/other'));assert not c.finish('')['web_requests']
results.append(dict(name='website_exact_path_scope',kind='contract_test',passed=True))
OUT.mkdir(parents=True,exist_ok=True);(OUT/'synthetic_identifiability_results.json').write_text(json.dumps(dict(scope='constructed worlds, not empirical attack trajectories',tests=len(results),all_passed=True,results=results),ensure_ascii=False,indent=2)+'\n');print(f'{len(results)} checks passed')
