"""Offline integration checks for candidate-bound plans and bounded replanning."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from src.engine.runner import Runner
from src.engine.storage import RunStore
from src.policies.stage_aware import StageAware
from src.schema import TargetResult
from tests.test_aligned_evidence_false import (
    AlignedEvidenceFalseTests as Fixtures, EvidenceDecision, POLICY, PLAN_ONLY,
)
from tests.test_piminer_aligned import FakeFactory


class ContinuityDecision(EvidenceDecision):
    def __call__(self, prompt, stage):
        result = super().__call__(prompt, stage)
        if stage == 'planner':
            plan = json.loads(result)
            memory = json.loads(prompt.split('\nPUBLIC INPUT:\n', 1)[1])['context']['revision_memory']
            plan['previous_plan_review'] = [dict(
                plan_id=outcome['plan_id'], assessment='inconclusive', decision='adjust',
                outcome_refs=outcome['outcome_refs'],
                reason='Visible feedback does not identify a cause; test a bounded adjustment.',
                preserve=['Keep the publicly specified goal arguments'])
                for outcome in memory.get('plan_outcomes', [])]
            # Omit optional empty extension for compatibility with the baseline.
            if not plan['previous_plan_review']:
                del plan['previous_plan_review']
            return json.dumps(plan)
        return result


class PlanContinuityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Fixtures.setUpClass()

    def test_runner_forwards_plan_to_the_next_generation(self):
        fixture = Fixtures()
        class Target:
            def execute(self, case, payload, episode_id):
                return TargetResult(False, final='PRIVATE_FINAL', black_box_final='Public outcome ' + episode_id)

        class Observer:
            def collect(self, episode_id, case, view):
                packets = deepcopy(fixture.packet_fixture)
                for packet in packets:
                    packet['episode_id'] = episode_id
                return packets

        decide = ContinuityDecision(fixture.case['injection_task_goal'])
        adapter = fixture.adapter(FakeFactory(), decide)
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Target(), adapter, Observer(), {POLICY: StageAware(unknown_action='revise')},
                RunStore(Path(directory) / 'run', {}), post_failure_budget=2)
            runner.run([deepcopy(fixture.case)])
        revisions = [g for g in runner.generations if g.get('revision_path') == 'grounded_false']
        self.assertEqual(len(revisions), 2)
        first, second = revisions
        self.assertEqual(second['status'], 'ready')
        capsule = first['revision_plan']
        self.assertEqual(capsule['payload_sha256'], first['payload_sha256'])
        self.assertEqual(capsule['summary']['hypothesis'], PLAN_ONLY)
        self.assertEqual(second['revision_memory']['attempts'][1]['revision_plan'], capsule)
        outcome = second['revision_memory']['plan_outcomes'][0]
        self.assertEqual(outcome['attempt_iters'], [1])
        self.assertIn('attempt:1:trigger', outcome['outcome_refs'])
        self.assertEqual(second['plan']['previous_plan_review'][0]['plan_id'], capsule['plan_id'])
        self.assertFalse(any(f['kind'] == 'revision_plan' for f in second['revision_memory']['facts']))

    def test_plan_survives_f_u_f_without_leaking_into_black_box_session(self):
        fixture = Fixtures()
        decide = ContinuityDecision(fixture.case['injection_task_goal'])
        with fixture.branch(decide=decide) as (adapter, factory, _, p0, history, _):
            p1, first = fixture.revise(adapter, p0, history, 1)
            history.append(dict(fixture.attempt(p1, first['analysis'], 1),
                                revision_plan=first['revision_plan']))
            p2, unknown = fixture.revise(adapter, p1, history, 2, 'U')
            fixture.assert_black_box(factory.sessions[0].prompts[0])
            history.append(fixture.attempt(p2, unknown['analysis'], 2))
            _, third = fixture.revise(adapter, p2, history, 3)
            self.assertEqual(third['revision_memory']['attempts'][1]['revision_plan'], first['revision_plan'])
            self.assertEqual(third['plan']['previous_plan_review'][0]['plan_id'], first['revision_plan']['plan_id'])

    def test_failed_advisory_replanning_preserves_valid_candidate_and_original_plan(self):
        fixture = Fixtures()
        class UnavailableReplan(ContinuityDecision):
            def __call__(self, prompt, stage):
                if stage == 'plan_revision':
                    raise RuntimeError('Offline unavailable planner')
                result = super().__call__(prompt, stage)
                if stage == 'semantic_review':
                    result['assessments']['plan_goal_alignment'] = 'conflict'
                    result['findings'] = [dict(dimension='plan_goal_alignment',
                        explanation='Fixture: reconsider plan.', quotes=[dict(source='plan', text=PLAN_ONLY)])]
                return result
        decide = UnavailableReplan(fixture.case['injection_task_goal'])
        with fixture.branch(decide=decide) as (adapter, _, _, p0, history, _):
            candidate, record = fixture.revise(adapter, p0, history, 1)
        self.assertEqual(candidate.text, 'F_CANDIDATE_1')
        self.assertEqual(record['status'], 'ready')
        self.assertEqual(record['plan_revisions'][0]['status'], 'unavailable')
        self.assertEqual(record['revision_plan']['summary']['hypothesis'], PLAN_ONLY)
        self.assertIn('unresolved_advisory_conflicts', record)

    def test_plan_conflict_revisits_planner_before_repairing_candidate(self):
        fixture = Fixtures()
        class ReplanDecision(ContinuityDecision):
            def __call__(self, prompt, stage):
                if stage == 'plan_revision':
                    plan = json.loads(super().__call__(prompt, 'planner'))
                    self.calls[-1]['stage'] = stage
                    plan['hypothesis'] = 'CORRECTED_PLAN'
                    return json.dumps(plan)
                result = super().__call__(prompt, stage)
                if stage == 'semantic_review' and self.generated == 1:
                    result['assessments']['plan_goal_alignment'] = 'conflict'
                    result['findings'] = [dict(dimension='plan_goal_alignment',
                        explanation='Fixture: revise the plan before following it.',
                        quotes=[dict(source='plan', text=PLAN_ONLY)])]
                return result

        decide = ReplanDecision(fixture.case['injection_task_goal'])
        with fixture.branch(decide=decide) as (adapter, _, _, p0, history, _):
            _, record = fixture.revise(adapter, p0, history, 1)
        self.assertEqual([c['stage'] for c in record['calls']],
            ['planner', 'generator', 'semantic_review', 'plan_revision', 'static_repair', 'semantic_review'])
        self.assertEqual(record['plan']['hypothesis'], 'CORRECTED_PLAN')
        self.assertEqual(record['revision_plan']['summary']['hypothesis'], 'CORRECTED_PLAN')
        self.assertEqual(record['plan_revisions'][0]['original_plan']['hypothesis'], PLAN_ONLY)
        repair = next(c['prompt'] for c in record['calls'] if c['stage'] == 'static_repair')
        self.assertIn('CORRECTED_PLAN', repair)
        self.assertNotIn(PLAN_ONLY, repair)


if __name__ == '__main__':
    unittest.main()
