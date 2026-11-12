"""Exact evidence ablation: keep the original Controller and mask observations."""
from dataclasses import replace
from .stage_aware import StageAware


class StageAwareNoEvidence(StageAware):
    def on_failure(self, history):
        masked = replace(history, completed=tuple(
            replace(episode, trigger='U',
                    stage=episode.stage if episode.stage in ('SUCCESS', 'ERROR') else 'UNOBSERVABLE',
                    evidence=tuple(replace(item, status='U') for item in episode.evidence))
            for episode in history.completed))
        decision = super().on_failure(masked)
        return replace(decision, reason='evidence_masked_' + decision.reason)
