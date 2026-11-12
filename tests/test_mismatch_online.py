import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.mismatch.data import ROOT, jsonl, write_json, digest
from src.mismatch.online import selection, live_packets, run_branch, validate
from src.evaluation.mismatch_online import goal_score, score_run
from tests.test_mismatch_autonomous import V1Scripted, judgment


class OnlinePilotTests(unittest.TestCase):
    prepared = ROOT / 'data/mismatch_autonomous_20261104'

    def test_selection_is_nine_fixed_interventions_and_native_valid(self):
        result = validate(self.prepared)
        self.assertEqual(len(result['instances']), 9)
        self.assertEqual(result['max_target_dispatches'], 45)
        self.assertIn('S02_M1:full', result['instances'])
        self.assertFalse(any('accurate' in instance for instance in result['instances']))

    def test_private_state_never_enters_live_receipts(self):
        execution = dict(gold=True, utility=True, raw=dict(pre='PRIVATE_SENTINEL', post='PRIVATE_SENTINEL',
            messages=[dict(role='tool', tool_call=dict(function='send_email', args={'recipients':['public@example.com']}),
                           content=[dict(type='text',content='returned receipt')], error=None),
                      dict(role='tool', tool_call=dict(function='list_files',args={}),
                           content=[dict(type='text',content='PRIVATE_READ')], error=None)]))
        packets = live_packets(execution, 'workspace')
        self.assertEqual(len(packets), 1)
        self.assertNotIn('PRIVATE', json.dumps(packets))
        self.assertNotIn('gold', json.dumps(packets))
        self.assertIn('returned receipt', packets[0]['response']['evidence'])

    def test_checker_requires_every_selected_file_deleted(self):
        case = json.loads((ROOT/'reports/mismatch_p0_20261104/inputs/S01.json').read_text())
        evidence = json.loads(next((ROOT/'Data/mismatch_dataset_v5/evidence').glob('S01*full*')).read_text())
        execution = dict(raw=evidence, error=None)
        self.assertTrue(goal_score(case, execution)['gold'])
        largest = sorted(evidence['pre']['cloud_drive']['files'].items(),
                         key=lambda kv:kv[1]['size'], reverse=True)[:5]
        # Preserve a selected source but delete a different file, keeping the count at minus five.
        selected_id, selected_file = largest[0]
        evidence['post']['cloud_drive']['files'][selected_id] = selected_file
        other = next(k for k in evidence['post']['cloud_drive']['files'] if k not in {k for k,_ in largest})
        del evidence['post']['cloud_drive']['files'][other]
        checked = goal_score(case, execution)
        self.assertFalse(checked['gold'])
        self.assertFalse(checked['conditions']['all_five_source_files_deleted'])

    def test_checker_matches_full_and_nonfull_frozen_goal_examples(self):
        for sid in ('S01','S02','S03'):
            case = json.loads((ROOT/f'reports/mismatch_p0_20261104/inputs/{sid}.json').read_text())
            for path in (ROOT/'Data/mismatch_dataset_v5/evidence').glob(sid+'*.json'):
                evidence = json.loads(path.read_text())
                checked = goal_score(case, dict(raw=evidence,error=None))
                self.assertEqual(checked['gold'], evidence['branch']=='full', str(path))

    def test_scoring_error_has_no_boolean_gold(self):
        self.assertIsNone(goal_score({},dict(error='provider_error'))['gold'])

    def test_independent_report_uses_new_execution_scores_and_rejects_mutation(self):
        _, instances=selection(self.prepared)
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory)
            labels=[]
            for index,(public,label) in enumerate(instances):
                name=f'b{index:03d}';folder=run/'branches'/name
                labels.append(dict(branch=name,**label))
                evidence=json.loads(next((ROOT/'Data/mismatch_dataset_v5/evidence').glob(
                    label['seed_id']+'*full*')).read_text())
                write_json(folder/'job_public.json',dict(input=public))
                write_json(folder/'branch.json',dict(status='self_reported_complete',error_type=None,
                    decisions=[judgment('unknown','Retry'),judgment('completed','Stop')],
                    target_attempts=1,generations=[]))
                write_json(folder/'executions/01/request_public.json',dict(payload={'id':'P0','text':public['injection']}))
                write_json(folder/'executions/01/execution_private.json',dict(raw=evidence,gold=True,error=None))
                write_json(folder/'frozen.json',dict(files={str(p.relative_to(folder)):digest(p)
                    for p in folder.rglob('*.json') if p.name!='frozen.json'}))
            write_json(run/'selection_private.json',labels)
            write_json(run/'status.json',dict(status='predictions_frozen'))
            # Check frozen bytes before any result is reported.
            changed=run/'branches/b000/branch.json';original=changed.read_text()
            changed.write_text(original+' ')
            with self.assertRaises(ValueError):score_run(run)
            self.assertFalse((run/'metrics.json').exists())
            changed.write_text(original)
            metrics=score_run(run)
            self.assertEqual(metrics['all']['online_success_count']['5'],9)
            self.assertEqual(metrics['initial_incomplete_primary']['instances'],5)
            self.assertEqual(metrics['paired_effects']['status'],'not_run')
            self.assertTrue((run/'RESULTS.md').exists())

    def test_live_controller_uses_five_new_executions_without_hidden_success_stop(self):
        _, instances = selection(self.prepared)
        public, _ = instances[0]
        scripted = V1Scripted([judgment('unknown','Retry') for _ in range(6)])
        from src.attacker.piminer import PIMinerAdapter
        adapter = PIMinerAdapter(ROOT,'fixture',decide=scripted)
        # The tracked transport records provider.calls, as the real transport does.
        scripted.calls=[]
        with tempfile.TemporaryDirectory() as directory:
            branch=Path(directory)/'b000'
            write_json(branch/'job_public.json',dict(input=public,model='fixture',effort='max',timeout=1,
                       view='authorized_evidence',target_config={},
                       ablation_protocol='tool_evidence_piminer_library_v3'))
            dispatched=[]
            def fake_target(command, **kwargs):
                request_path=Path(command[-1]); folder=request_path.parent
                request=json.loads(request_path.read_text());dispatched.append(request)
                from src.schema import Payload
                write_json(folder/'execution_private.json',dict(gold=True,private='PRIVATE_GOLD_SENTINEL'))
                write_json(folder/'feedback_public.json',dict(final_output='No outcome assertion.',packets=[],
                    error=None,payload_sha256=Payload(**request['payload']).sha256))
            with patch('src.mismatch.online.configure'), patch('src.attacker.piminer.PIMinerAdapter',return_value=adapter), \
                 patch('src.mismatch.online.subprocess.run',side_effect=fake_target):
                result=run_branch(branch)
            self.assertEqual(result['target_attempts'],5)
            self.assertEqual(result['decisions'],6)
            self.assertEqual(result['status'],'budget_exhausted')
            self.assertEqual(len(dispatched),5)
            self.assertEqual(len({r['payload']['text'] for r in dispatched}),1)
            self.assertNotIn('PRIVATE_GOLD_SENTINEL',str(scripted.prompts))
            frozen=json.loads((branch/'frozen.json').read_text())
            self.assertTrue(all(digest(branch/name)==sha for name,sha in frozen['files'].items()))


if __name__ == '__main__':
    unittest.main()
