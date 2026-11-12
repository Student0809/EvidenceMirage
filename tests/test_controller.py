from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from src.schema import Action, Payload, TargetResult
from src.engine.budget import Budget
from src.engine.history import History, ObservationSummary
from src.engine.runner import Runner
from src.engine.storage import RunStore
from src.engine.config import ROOT, load_config, load_cases
from src.evidence.ledger import EvidenceLedger
from src.evidence.observer import FixedObserver, policy_for_case
from src.evidence.stage import estimate_stage
from src.vendor.attacker_observer import Collector
from src.policies.registry import build_policies
from src.policies.stage_aware import StageAware
from src.adapters.fixture import FixtureAttacker


def history(states, hashes=None, remaining=3):
    hashes = hashes or ['same'] * len(states)
    rows = tuple(ObservationSummary(str(i), h, estimate_stage(False, s), s, 'Partial')
                 for i, (s, h) in enumerate(zip(states, hashes)))
    return History(rows, remaining, len(states)-1)


class PolicyTests(unittest.TestCase):
    def test_trigger_history_and_retry_cap(self):
        p = StageAware()
        for states, expected in [(['T'], Action.RETRY), (['T','T'], Action.RETRY),
                                 (['T','T','T'], Action.REVISE), (['T','F'], Action.REVISE),
                                 (['F'], Action.REVISE), (['U'], Action.RETRY),
                                 (['U','U'], Action.REVISE)]:
            self.assertEqual(p.decide(history(states)).action, expected)

    def test_identical_revision_cannot_reset_payload_cap(self):
        self.assertEqual(StageAware().decide(history(['T','T','T'], ['x','x','x'])).action, Action.REVISE)
        self.assertEqual(StageAware().decide(history(['T','T','T'], ['x','x','y'])).action, Action.RETRY)

    def test_all_policies_stop_on_success_error_or_budget(self):
        policies = build_policies(load_config(ROOT/'experiments/e0_smoke.yaml')['policies'])
        for p in policies.values():
            for stage in ('SUCCESS','ERROR'):
                h = History((ObservationSummary('e','h',stage,'T','Reject'),),3,0)
                self.assertEqual(p.decide(h).action, Action.STOP)
            self.assertEqual(p.decide(history(['T'],remaining=0)).action, Action.STOP)

    def test_final_label_does_not_override_trigger(self):
        h = History((ObservationSummary('e','h','TRIGGERED_FAIL','T','Reject'),),3,0)
        policies=build_policies(load_config(ROOT/'experiments/e0_smoke.yaml')['policies'])
        self.assertEqual(policies['stage_aware'].decide(h).action,Action.RETRY)
        self.assertEqual(policies['label_based'].decide(h).action,Action.REVISE)

    def test_policy_input_is_immutable_and_cannot_contain_future_raw(self):
        h=history(['T'])
        with self.assertRaises(Exception): h.completed=()
        self.assertFalse(hasattr(h,'raw'))
        self.assertEqual(StageAware().decide(h),StageAware().decide(h))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.case=load_cases(load_config(ROOT/'experiments/e0_smoke.yaml'))[5]

    def ledger(self, attachments=None):
        collector=Collector(policy_for_case(self.case))
        if attachments is not None:
            collector.mail(collector.policy['mailboxes'],'x','y',attachments)
        ledger=EvidenceLedger('e',self.case['goal_spec'])
        for packet in FixedObserver().collect('e',self.case,collector.finish('I refuse.')):
            ledger.update(packet)
        return ledger

    def test_absence_unknown_and_scoped_false(self):
        self.assertEqual(self.ledger().trigger,'U')
        self.assertEqual(self.ledger([]).trigger,'F')
        self.assertEqual(self.ledger(['file']).trigger,'T')

    def test_gold_is_sole_success(self):
        self.assertEqual(estimate_stage(False,'T'),'TRIGGERED_FAIL')
        self.assertEqual(estimate_stage(True,'U'),'SUCCESS')
        self.assertEqual(estimate_stage(None,'T','Timeout'),'ERROR')
        with self.assertRaises(ValueError): TargetResult(False,error='Timeout')

    def test_foreign_and_conflicting_evidence_rejected(self):
        ledger=self.ledger(['x']); p=deepcopy(next(iter(ledger.packets.values())))
        p['episode_id']='future'
        with self.assertRaises(ValueError):ledger.update(p)
        p['episode_id']='e';p['policy']='other'
        with self.assertRaises(ValueError):ledger.update(p)


class ScriptTarget:
    def __init__(self, sequence):
        self.sequence=sequence;self.calls=[]

    def execute(self, case, payload, eid):
        self.calls.append((eid,payload.sha256))
        idx=int(eid.rsplit('/',1)[-1])
        value=self.sequence[min(idx,len(self.sequence)-1)]
        if value=='error':return TargetResult(None,error='Timeout')
        c=Collector(policy_for_case(case))
        c.mail(c.policy['mailboxes'],'x','y',['file'])
        return TargetResult(value,final='I cannot comply.',observation=c.finish('I cannot comply.'))


class FailingAttacker:
    def generate(self,*args):raise RuntimeError('generation unavailable')


class RunnerTests(unittest.TestCase):
    def run_case(self, sequence, attacker=None, budget=3):
        cfg=load_config(ROOT/'experiments/e0_smoke.yaml')
        case=load_cases(cfg)[0]
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        target=ScriptTarget(sequence)
        runner=Runner(target,attacker or FixtureAttacker(),FixedObserver(),build_policies(cfg['policies']),
                      RunStore(Path(directory.name)/'run',{}),post_failure_budget=budget)
        metrics=runner.run([case])
        return runner,target,metrics

    def test_h1_shared_once_and_success_stops_every_arm(self):
        r,t,m=self.run_case([False,True])
        self.assertEqual(len(t.calls),6)
        self.assertEqual(m['physical_initial_executions'],1)
        for group in m['policies'].values():
            self.assertEqual(group['confirmed_gold_asr_at_budget'],1)
            self.assertEqual(group['physical_post_failure_cost']['target_executions'],1)
        retry=[h for eid,h in t.calls if '/always_retry/' in eid]
        self.assertEqual(retry,[t.calls[0][1]])
        transitions=m['policies']['always_revise']['diagnostics']['trigger_transitions']
        self.assertEqual(len(transitions),1)
        self.assertTrue(transitions[0]['revised'])

    def test_initial_success_never_generates_or_retries(self):
        r,t,m=self.run_case([True],FailingAttacker())
        self.assertEqual(len(t.calls),1)
        self.assertEqual(len(r.generations),0)
        for g in m['policies'].values():self.assertIsNone(g['confirmed_gold_asr_at_budget'])

    def test_strict_budget_and_no_future_history_ids(self):
        r,t,m=self.run_case([False])
        self.assertEqual(len(t.calls),16)
        for d in r.decisions:
            self.assertEqual(len(d['visible_history_ids']),d['spent_target_budget']+1)
            self.assertLessEqual(d['spent_target_budget'],3)
        self.assertEqual(m['physical_total_cost']['target_usage_missing_executions'],16)

    def test_target_error_consumes_budget_but_never_false_gold(self):
        r,t,m=self.run_case([False,'error'])
        self.assertEqual(len(t.calls),6)
        self.assertTrue(all(e.gold is None for e in r.episodes[1:]))
        for g in m['policies'].values():
            self.assertEqual(g['post_failure_invalid_outcomes'],1)
            self.assertIsNone(g['gold_asr_at_budget_complete'])
            self.assertIsNone(g['gold_asr_valid_outcomes'])

    def test_generation_errors_are_terminal_and_do_not_consume_target(self):
        r,t,m=self.run_case([False],FailingAttacker())
        g=m['policies']['always_revise']
        self.assertEqual(g['statuses'],{'GENERATION_ERROR':1})
        self.assertEqual(g['physical_post_failure_cost']['target_executions'],0)
        self.assertIsNone(g['gold_asr_at_budget_complete'])
        self.assertEqual(g['diagnostics']['actions'],{'Revise':1,'Stop':1})

    def test_initial_error_keeps_case_and_never_generates(self):
        r,t,m=self.run_case(['error'],FailingAttacker())
        self.assertEqual(len(t.calls),1)
        for g in m['policies'].values():
            self.assertEqual(g['initial_invalid'],1)
            self.assertEqual(g['first_failure_cases'],0)

    def test_budget_zero_and_run_overwrite_refused(self):
        r,t,m=self.run_case([False],budget=0)
        self.assertEqual(len(t.calls),1)
        with self.assertRaises(FileExistsError):RunStore(r.store.path,{})


if __name__=='__main__': unittest.main()
