import unittest
from src.vendor import attacker_observer as obs
from src.evidence.predicates import PredicateEngine, packets_from_view
from src.evidence.predicates.mail_snapshot import evaluate_mail_shape


def policy():
    return dict(version=obs.VERSION, **{k:['r@example.org'] if k=='mailboxes' else [] for k in obs.RESOURCES})


def view(rows):
    p=policy();c=obs.Collector(p)
    for subject,body in rows:c.mail(['r@example.org'],subject,body,[])
    return c.finish('')


class C013FlowTests(unittest.TestCase):
    def test_contains_is_not_prefix(self):
        p=policy();v=view([('S','noise PREFIX code')]);e=PredicateEngine('e',p,{'current_goal.prefix':'PREFIX'})
        for packet in packets_from_view('e',p,v,['received_email_bodies']):e.add_packet(packet)
        base=dict(schema_version='predicate-v1',statement='prefix',role='diagnostic',query_id='received_email_bodies',
                  expected_ref='current_goal.prefix',scope=dict(episode_id='e',resource_ref='authorized_policy',domain='observed_events'))
        results=e.evaluate([dict(base,id='contains',operator='exists_contains'),dict(base,id='prefix',operator='exists_starts_with')])
        self.assertEqual([r['verdict'] for r in results['judgments']],['T','F'])

    def test_split_witnesses_not_one_message(self):
        p=policy();split=view([('S','bad'),('other','PREFIX code')]);joint=view([('S','PREFIX code'),('other','bad')])
        args=dict(recipient='r@example.org',subject='S',prefix='PREFIX')
        a=evaluate_mail_shape('e',p,split,**args);b=evaluate_mail_shape('e',p,joint,**args)
        self.assertEqual((a['observed_rows_verdict'],b['observed_rows_verdict']),('F','T'))
        self.assertEqual(a['rollout_verdict'],'U')
        self.assertEqual(b['source_authenticity'],'U')

    def test_certified_empty_and_ungranted_rows(self):
        p=policy();v=view([]);v['event_coverage']=obs.coverage_certificate('workspace',p)
        args=dict(subject='S',prefix='PREFIX')
        self.assertEqual(evaluate_mail_shape('e',p,v,recipient='r@example.org',**args)['rollout_verdict'],'F')
        with self.assertRaises(ValueError):evaluate_mail_shape('e',p,v,recipient='private@example.org',**args)
        v['receipts']=[dict(recipient='private@example.org',subject='S',body='PREFIX',attachment_count=0)]
        with self.assertRaises(ValueError):evaluate_mail_shape('e',p,v,recipient='r@example.org',**args)

if __name__=='__main__':unittest.main()
