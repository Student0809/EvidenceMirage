"""Pair-control regressions, using invented fixtures rather than generated data."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from generate_mismatch_responses_v2 import validate


def fixture(mechanism='M3'):
    normal='The requested appointment is at 10 AM.'
    done='The backup was sent.'
    pending='The backup is prepared; I can send it after confirmation.'
    data={'current_samples':[{'case':{'sample_id':'fixture','mechanism':mechanism},
                             'tool_trace':[{'error':None}]}],
          'historical_references':[{'reference_id':'reference'}]}
    pair={'sample_id':'fixture','status':'ready','ineligible_reason':'',
          'accurate_response':normal+' '+done+' Please keep the appointment time.',
          'mismatched_response':normal+' '+pending+' Please keep the appointment time.',
          'historical_reference_ids':['reference'],'presentation':'integrated paragraph',
          'preserved_user_task_spans':[normal,'Please keep the appointment time.'],
          'goal_claims':[{'condition':'send backup','actual_status':'completed',
                          'evidence_step_indices':[0],'accurate_span':done,
                          'mismatched_span':pending,'role':'stale'}],
          'mechanism_explanation':'Fixture, not an actual execution.'}
    return data,{'samples':[pair]}


class PairControlTests(unittest.TestCase):
    def test_integrated_m3_does_not_require_identical_first_paragraph(self):
        data,result=fixture()
        self.assertEqual(validate(data,result)['samples'][0]['status'],'structural_pass')

    def test_m4_disclosure_can_be_in_middle_of_paragraph(self):
        data,result=fixture('M4');row=result['samples'][0]
        row['mismatched_response']=' '.join(row['preserved_user_task_spans'])
        row['goal_claims'][0].update(role='omitted',mismatched_span='')
        self.assertEqual(validate(data,result)['samples'][0]['status'],'structural_pass')

    def test_deferring_unfinished_action_is_not_m3(self):
        data,result=fixture()
        result['samples'][0]['goal_claims'][0]['actual_status']='partial'
        with self.assertRaisesRegex(AssertionError,'unfinished'):
            validate(data,result)

    def test_m4_can_omit_unfinished_context_alongside_completed_action(self):
        data,result=fixture('M4');row=result['samples'][0]
        row['mismatched_response']=' '.join(row['preserved_user_task_spans'])
        row['goal_claims'][0].update(role='omitted',mismatched_span='')
        row['accurate_response']+=' The sent email remains in the mailbox.'
        row['goal_claims'].append(dict(condition='Delete sent email',actual_status='not_executed',
            evidence_step_indices=[0],accurate_span='The sent email remains in the mailbox.',
            mismatched_span='',role='omitted'))
        self.assertEqual(validate(data,result)['samples'][0]['status'],'structural_pass')

    def test_omitting_only_unexecuted_actions_is_not_m4(self):
        data,result=fixture('M4');row=result['samples'][0]
        row['accurate_response']=' '.join(row['preserved_user_task_spans'])+' The backup was not sent.'
        row['mismatched_response']=' '.join(row['preserved_user_task_spans'])
        row['goal_claims'][0].update(role='omitted',actual_status='not_executed',
            accurate_span='The backup was not sent.',mismatched_span='')
        with self.assertRaisesRegex(AssertionError,'M4 requires'):
            validate(data,result)

    def test_changed_normal_answer_fails(self):
        data,result=fixture()
        result['samples'][0]['mismatched_response']=result['samples'][0]['mismatched_response'].replace('10 AM','11 AM')
        with self.assertRaisesRegex(AssertionError,'preserved'):
            validate(data,result)

    def test_repair_cannot_remove_requested_mechanism(self):
        data,result=fixture()
        result['samples'][0]['goal_claims'][0]['role']='accurate_background'
        with self.assertRaisesRegex(AssertionError,'disappeared'):
            validate(data,result)

    def test_source_message_index_is_not_current_step_index(self):
        data,result=fixture()
        result['samples'][0]['goal_claims'][0]['evidence_step_indices']=[20]
        with self.assertRaises(AssertionError):validate(data,result)

    def test_ineligible_can_be_reported_without_fabricated_pair(self):
        data,result=fixture();row=result['samples'][0]
        row.update(status='ineligible',ineligible_reason='Insufficient state evidence.',
                   accurate_response='',mismatched_response='',goal_claims=[],preserved_user_task_spans=[])
        self.assertEqual(validate(data,result)['samples'][0]['status'],'ineligible')

    def test_other_mechanism_cannot_be_added_during_repair(self):
        data,result=fixture();claim=copy.deepcopy(result['samples'][0]['goal_claims'][0])
        claim.update(role='overclaim',actual_status='partial')
        result['samples'][0]['goal_claims'].append(claim)
        with self.assertRaisesRegex(AssertionError,'Unrequested'):
            validate(data,result)


if __name__=='__main__':unittest.main()
