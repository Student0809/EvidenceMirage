"""P0 confirmation must buy repeat evidence without expanding Target budget."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from src.engine.config import ROOT, load_cases, load_config
from src.engine.history import History, ObservationSummary
from src.engine.runner import Runner
from src.engine.storage import RunStore
from src.evidence.observer import FixedObserver, policy_for_case
from src.evidence.stage import estimate_stage
from src.policies.stage_aware import StageAware
from src.policies.stage_aware_p0_confirm import StageAwareP0Confirm
from src.schema import Action, Payload, TargetResult
from src.vendor.attacker_observer import Collector


def history(states, *, ids=None, hashes=None, remaining=None):
    n = len(states)
    ids = ['P0']*n if ids is None else ids
    hashes = ['initial']*n if hashes is None else hashes
    remaining = 5-n if remaining is None else remaining
    return History(tuple(ObservationSummary(str(i), sha, estimate_stage(False, state),
                         state, 'Ignored', payload_id=pid)
                         for i,(state,pid,sha) in enumerate(zip(states,ids,hashes))), remaining, n-1)


class P0PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = StageAwareP0Confirm()

    def test_confirm_first_false_then_revise_two_false(self):
        self.assertEqual(self.policy.decide(history(['F'])).action, Action.RETRY)
        decision = self.policy.decide(history(['F','F']))
        self.assertEqual(decision.action, Action.REVISE)
        self.assertEqual(decision.reason, 'p0_confirmed_trigger_false')
        self.assertEqual(StageAware().decide(history(['F'])).action, Action.REVISE)

    def test_mixed_history_order_does_not_discard_positive(self):
        for states in (['T','F'],['F','T']):
            self.assertEqual(self.policy.decide(history(states)).action, Action.RETRY)
            self.assertEqual(self.policy.decide(history(states)).reason,'p0_mixed_trigger_confirmation')
            for final in ('T','F','U'):
                self.assertEqual(self.policy.decide(history(states+[final])).action, Action.REVISE)

    def test_three_execution_cap_also_applies_with_large_budget(self):
        for states in (['T']*3,['F']*3,['U']*3,['T','F','U']):
            self.assertEqual(self.policy.decide(history(states,remaining=8)).reason,'p0_execution_cap')

    def test_retry_requires_two_remaining_revision_executions(self):
        for states in (['F'],['T','F'],['T']):
            self.assertEqual(self.policy.decide(history(states,remaining=3)).action,Action.RETRY)
            self.assertEqual(self.policy.decide(history(states,remaining=2)).reason,'p0_reserve_revision_budget')
            self.assertEqual(self.policy.decide(history(states,remaining=1)).action,Action.REVISE)

    def test_unknown_observer_error_and_all_positive_follow_baseline(self):
        for states in (['U'],['U','U'],['U','F'],['F','U'],['T','U'],['U','T'],['T','T']):
            self.assertEqual(self.policy.decide(history(states)),StageAware().decide(history(states)))
        h=history(['U'])
        h=replace(h,completed=(replace(h.last,observation_error=True),))
        self.assertEqual(self.policy.decide(h),StageAware().decide(h))

    def test_revisions_never_reactivate_confirmation_even_for_identical_bytes(self):
        for h in (history(['F','F'],ids=['P0','P1']),
                  history(['F','F','F'],ids=['P0','P1','P2'],hashes=['initial','new','initial']),
                  history(['F'],ids=['P1']),history(['F'],ids=[None])):
            self.assertEqual(self.policy.decide(h),StageAware().decide(h))

    def test_success_error_and_zero_budget_override_confirmation(self):
        for stage in ('SUCCESS','ERROR'):
            h=history(['F']);h=replace(h,completed=(replace(h.last,stage=stage),))
            self.assertEqual(self.policy.decide(h).action,Action.STOP)
        self.assertEqual(self.policy.decide(history(['F'],remaining=0)).action,Action.STOP)

    def test_policy_ignores_response_label_and_does_not_mutate_evidence(self):
        original=history(['T','F']);before=original.completed
        changed=replace(original,completed=tuple(replace(e,response_signal='Reject') for e in original.completed))
        self.assertEqual(self.policy.decide(original),self.policy.decide(changed))
        self.assertEqual(original.completed,before)


class SequenceTarget:
    def __init__(self, states):
        self.states=states;self.calls=[];self.views=[]
    def execute(self,case,payload,eid):
        self.calls.append((eid,payload.id,payload.sha256))
        state=self.states[min(len(self.calls)-1,len(self.states)-1)]
        if state=='error':return TargetResult(None,error='OfflineTimeout')
        collector=Collector(policy_for_case(case))
        if state!='U':
            collector.mail(collector.policy['mailboxes'],'subject','body',['file'] if state in ('T','success') else [])
        view=collector.finish('Done.')
        self.views.append(view)
        return TargetResult(state=='success',final='Done.',observation=view)


class RevisionAttacker:
    def __init__(self,fail=False):self.calls=[];self.fail=fail
    def generate(self,case,payload,last,packets,revision):
        self.calls.append((payload.id,last.episode_id,revision))
        if self.fail:raise RuntimeError('OfflineGenerationFailure')
        result=Payload(revision,'distinct revised candidate '+revision)
        return result,dict(status='ready',calls=[])


class P0RunnerTests(unittest.TestCase):
    def run_sequence(self,states,attacker=None,policies=None):
        case=load_cases(load_config(ROOT/'experiments/e0_smoke.yaml'))[5]
        case['p0']='fixed initial candidate'
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        target=SequenceTarget(states);attacker=attacker or RevisionAttacker()
        policies=policies or {'stage_aware_p0_confirm':StageAwareP0Confirm()}
        runner=Runner(target,attacker,FixedObserver(),policies,RunStore(Path(tmp.name)/'run',{}),post_failure_budget=4)
        metrics=runner.run([case])
        return runner,target,attacker,metrics

    def test_ff_uses_fresh_rollout_then_three_revised_candidates(self):
        r,t,a,m=self.run_sequence(['F']*5)
        self.assertEqual([x[1] for x in t.calls],['P0','P0','P1','P2','P3'])
        self.assertEqual([e.trigger for e in r.episodes],['F']*5)
        self.assertEqual(len({e.episode_id for e in r.episodes}),5)
        self.assertEqual(len({id(v) for v in t.views}),5)
        self.assertEqual(len(a.calls),3)
        self.assertEqual(m['physical_total_cost']['target_executions'],5)
        self.assertEqual(r.decisions[1]['reason'],'p0_confirmed_trigger_false')

    def test_tf_or_ft_gets_exactly_third_p0_then_two_revision_slots(self):
        for states in (['T','F','F','F','F'],['F','T','F','F','F']):
            r,t,a,m=self.run_sequence(states)
            self.assertEqual([x[1] for x in t.calls],['P0','P0','P0','P1','P2'])
            self.assertEqual(r.decisions[2]['remaining_target_budget'],2)
            self.assertEqual(r.decisions[2]['reason'],'p0_execution_cap')
            self.assertEqual(len(a.calls),2)

    def test_success_and_error_stop_without_forcing_reserved_trials(self):
        for state in ('success','error'):
            r,t,a,m=self.run_sequence(['F',state])
            self.assertEqual(len(t.calls),2)
            self.assertEqual(len(a.calls),0)
            self.assertEqual(r.decisions[-1]['action'],'Stop')
            self.assertIs(r.episodes[-1].gold,True if state=='success' else None)

    def test_failed_revision_does_not_spend_target_budget(self):
        r,t,a,m=self.run_sequence(['F']*5,RevisionAttacker(fail=True))
        self.assertEqual(len(t.calls),2)
        self.assertEqual(r.outcomes[0]['status'],'GENERATION_ERROR')
        self.assertIsNone(r.outcomes[0]['gold_success'])

    def test_renamed_p0_revision_is_rejected_without_target_or_generation_retry(self):
        class DuplicateAttacker(RevisionAttacker):
            def generate(self,case,payload,last,packets,revision):
                self.calls.append(revision)
                return Payload(revision,case['p0']),dict(status='ready',calls=[dict(stage='revision')])
        for states in (['F','F'],['T','F','F']):
            r,t,a,m=self.run_sequence(states,DuplicateAttacker())
            self.assertEqual(len(t.calls),len(states))
            self.assertEqual(len(a.calls),1)
            self.assertEqual(r.outcomes[0]['status'],'GENERATION_ERROR')
            self.assertIsNone(r.outcomes[0]['gold_success'])
            self.assertEqual(r.generations[0]['error_type'],'RevisionMatchesInitialPayload')
            self.assertEqual(len(r.generations[0]['calls']),1)
            self.assertEqual(r.decisions[-1]['reason'],'GENERATION_ERROR')
        # The original baseline has no new candidate rejection rule.
        r,t,a,m=self.run_sequence(['F']*5,DuplicateAttacker(),{'stage_aware':StageAware()})
        self.assertEqual(len(t.calls),5)
        self.assertEqual(r.outcomes[0]['status'],'VALID_GOLD_FALSE')

    def test_later_revision_cannot_return_to_initial_bytes(self):
        class ReturningAttacker(RevisionAttacker):
            def generate(self,case,payload,last,packets,revision):
                self.calls.append(revision)
                text='different P1' if revision=='P1' else case['p0']
                return Payload(revision,text),dict(status='ready',calls=[])
        r,t,a,m=self.run_sequence(['F']*5,ReturningAttacker())
        self.assertEqual([x[1] for x in t.calls],['P0','P0','P1'])
        self.assertEqual(a.calls,['P1','P2'])
        self.assertEqual(r.generations[-1]['error_type'],'RevisionMatchesInitialPayload')
        self.assertEqual(r.outcomes[0]['status'],'GENERATION_ERROR')
        saved=[json.loads(p.read_text()) for p in r.store.path.rglob('*.generation.json')]
        self.assertEqual(sum(x['status']=='GENERATION_ERROR' for x in saved),1)

    def test_beta_records_only_available_past_and_isolated_payloads(self):
        r,t,a,m=self.run_sequence(['T','F','F','F','F'])
        self.assertEqual([(d['trigger_posterior']['nT'],d['trigger_posterior']['nF']) for d in r.decisions],
                         [(1,0),(1,1),(1,2),(0,1),(0,1)])
        for decision in r.decisions:
            stat=decision['trigger_posterior']
            self.assertTrue(stat['reporting_only'])
            self.assertFalse(stat['used_for_decisions'])
            self.assertTrue(set(stat['episode_ids'])<=set(decision['visible_history_ids']))
            self.assertNotIn('trigger_posterior',decision['history'])
        groups=m['policies']['stage_aware_p0_confirm']['trigger_posteriors']
        self.assertEqual(sorted(g['total'] for g in groups),[1,1,3])

    def test_changed_or_unavailable_beta_statistics_do_not_change_execution(self):
        baseline=self.run_sequence(['T','F','F','F','F'])
        expected=[(d['action'],d['reason']) for d in baseline[0].decisions]
        variants=[dict(mean=0.0),dict(mean=1.0),dict(mean=float('nan')),
                  dict(mean=float('inf')),dict(mean=object())]
        for value in variants:
            with patch('src.evaluation.beta_bernoulli.trigger_statistics',return_value=value):
                r,t,a,m=self.run_sequence(['T','F','F','F','F'])
            self.assertEqual([(d['action'],d['reason']) for d in r.decisions],expected)
            self.assertEqual(t.calls,baseline[1].calls)
        with patch('src.evaluation.beta_bernoulli.trigger_statistics',side_effect=RuntimeError('offline reporting failure')):
            r,t,a,m=self.run_sequence(['T','F','F','F','F'])
        self.assertEqual([(d['action'],d['reason']) for d in r.decisions],expected)
        self.assertEqual(t.calls,baseline[1].calls)
        self.assertTrue(all(d['trigger_posterior']['status']=='unavailable' for d in r.decisions))

    def test_baseline_remains_immediate_revise_and_branches_are_isolated(self):
        policies={'stage_aware':StageAware(),'stage_aware_p0_confirm':StageAwareP0Confirm()}
        r,t,a,m=self.run_sequence(['F']*9,policies=policies)
        self.assertEqual(len(t.calls),9) # shared H1 + four in each branch
        for name,expected in [('stage_aware',['P1','P2','P3','P4']),('stage_aware_p0_confirm',['P0','P1','P2','P3'])]:
            self.assertEqual([pid for eid,pid,_ in t.calls if '/'+name+'/' in eid],expected)
        self.assertTrue(all(len(d['visible_history_ids'])==d['spent_target_budget']+1 for d in r.decisions))


if __name__=='__main__':unittest.main()
