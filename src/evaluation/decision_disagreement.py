"""Read-only first-action comparisons; never manufacture counterfactual trajectories.

Actual comparisons require the same recorded Target execution, not merely equal
Payload/Gold/Trigger. Replays use only observed prefixes, and stop as soon as the
pair disagrees or their common action leaves the observed source trajectory.
"""
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

from ..engine.budget import Budget
from ..engine.history import History
from ..policies.registry import build_policies
from ..schema import Episode

ALIASES = {'B0': 'always_revise', 'B1': 'stage_aware', 'B2': 'stage_aware_p0_confirm'}
IDENTITY_FIELDS = ('case_id', 'suite_name', 'user_task_id', 'injection_task_id',
                   'base_task_id', 'user_task_prompt', 'injection_task_goal',
                   'context_with_placeholder', 'injection_candidates',
                   'resource_grants', 'goal_spec')


@dataclass
class DecisionCase:
    case_id: str
    policy_id: str
    total_target_budget: int
    source: str
    episodes: tuple = ()
    decisions: tuple = ()
    execution_origins: tuple = ()
    state_fingerprints: tuple = ()
    identity: dict = field(default_factory=dict)
    recording_status: str = 'unknown'
    outcome_status: str | None = None
    issues: tuple = ()
    warnings: tuple = ()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _json(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def _jsonl(path, warnings):
    if not path.is_file():
        return []
    lines = path.read_text().splitlines()
    result = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines)-1:
                warnings.append('incomplete_jsonl_tail:' + path.name)
            else:
                raise ValueError('Malformed committed JSONL: ' + str(path))
    return result


def _raw(directory, episode):
    path = (directory / episode.raw_path).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError('missing_or_unsafe_raw_path')
    data = path.read_bytes()
    raw = json.loads(data)
    if (hashlib.sha256(raw['payload'].encode()).hexdigest() != episode.payload_sha256
            or raw['result']['gold'] != episode.gold
            or raw['result'].get('error') != episode.error):
        raise ValueError('raw_episode_mismatch')
    return path, data, raw


def _origin(directory, episode, events, manifest, raw_bytes):
    """Known native origins only; unsupported reuse stays explicitly incomparable.

    A reused source is accepted only with the explicit legacy initial provenance,
    exact episode/raw bytes, and a matching recorded source completion. Recovery
    chains are deliberately not inferred from identical content or filenames.
    """
    eid = episode.episode_id
    markers = [e for e in events if e.get('episode_id') == eid and
               (e.get('event') in ('INITIAL_EXECUTION_REUSED', 'PREFIX_EXECUTION_RECOVERED')
                or e.get('record_origin'))]
    if markers:
        reused = [e for e in markers if e.get('event') == 'INITIAL_EXECUTION_REUSED']
        if len(markers) != 1 or len(reused) != 1 or manifest.get('reused_initial') is not True:
            return None
        provenance = manifest.get('initial_provenance', {})
        if 'source_case' not in provenance:
            provenance = provenance.get(episode.case_id, {})
        if provenance.get('episode_id') != eid or not provenance.get('source_case'):
            return None
        source = Path(provenance['source_case']).resolve()
        if (source == directory.resolve() or not manifest.get('source_run')
                or Path(manifest['source_run']).resolve() != source.parent):
            return None
        source_rows = _jsonl(source/'episodes.jsonl', [])
        matches = [row for row in source_rows if row.get('episode_id') == eid]
        if len(matches) != 1 or matches[0] != episode.to_dict():
            # JSON lists and in-memory tuples normalize via canonical JSON below.
            if len(matches) != 1 or _digest(matches[0]) != _digest(episode.to_dict()):
                return None
        _, source_bytes, _ = _raw(source, episode)
        if source_bytes != raw_bytes or reused[0].get('source_sha256') != hashlib.sha256(raw_bytes).hexdigest():
            return None
        source_events = _jsonl(source/'events.jsonl', [])
        if any(e.get('event') in ('INITIAL_EXECUTION_REUSED', 'PREFIX_EXECUTION_RECOVERED')
               or e.get('record_origin') for e in source_events if e.get('episode_id') == eid):
            return None
        directory, events = source, source_events
    starts = [e for e in events if e.get('event') == 'TARGET_STARTED' and e.get('episode_id') == eid]
    ends = [e for e in events if e.get('event') == 'TARGET_COMPLETED' and e.get('episode_id') == eid]
    if (len(starts) != 1 or len(ends) != 1 or starts[0].get('payload_sha256') != episode.payload_sha256
            or ends[0].get('status') != episode.status):
        return None
    return (str(directory.resolve()), eid)


def load_recorded_cases(run_path, policy_id=None):
    """Load a selected actual branch, without API calls or writing artifacts.

    Supports per-case workers and a flat Runner store. Raw bytes and full episode
    observations are fingerprinted internally; returned reports expose no text.
    Missing workers, decisions, and partial JSONL tails remain visible.
    """
    run = Path(run_path).resolve()
    manifest = _json(run/'manifest.json')
    if not isinstance(manifest, dict):
        raise ValueError('Missing run manifest')
    policies = manifest['config']['policies']
    policy_id = ALIASES.get(policy_id, policy_id)
    if policy_id is None:
        if len(policies) != 1:
            raise ValueError('Select a policy for a multi-arm run')
        policy_id = next(iter(policies))
    if policy_id not in policies:
        raise ValueError('Policy is not recorded in this run')
    limit = manifest['config']['post_failure_target_budget']
    Budget(limit)
    identities = {c['case_id']: c for c in manifest.get('cases', [])}
    case_ids = manifest.get('case_ids', list(identities))
    if len(case_ids) != len(set(case_ids)):
        raise ValueError('Duplicate manifest case IDs')
    result = {}
    for cid in case_ids:
        if not isinstance(cid, str) or '/' in cid or cid in ('.', '..'):
            raise ValueError('Unsafe case ID')
        child = run/cid
        directory = child if child.is_dir() or not (run/'episodes.jsonl').is_file() else run
        cm = _json(directory/'manifest.json', manifest if directory == run else {})
        warnings, issues = [], []
        if cm and directory != run:
            child_cases = {c.get('case_id'): c for c in cm.get('cases', [])}
            if (cm.get('config', {}).get('post_failure_target_budget') != limit
                    or policy_id not in cm.get('config', {}).get('policies', {})
                    or child_cases.get(cid) != identities.get(cid)):
                issues.append('child_manifest_mismatch')
        try:
            rows = [e for e in _jsonl(directory/'episodes.jsonl', warnings)
                    if e.get('case_id') == cid and e.get('policy_id') in ('shared', policy_id)]
            episodes = tuple(Episode(**row) for row in rows)
            decisions = tuple(d for d in _jsonl(directory/'decisions.jsonl', warnings)
                              if d.get('case_id') == cid and d.get('policy_id') == policy_id)
            events = _jsonl(directory/'events.jsonl', warnings)
        except (ValueError, TypeError, KeyError) as exc:
            episodes, decisions, events = (), (), []
            issues.append('invalid_record:' + type(exc).__name__)
        if cid not in identities:
            issues.append('missing_case_identity')
        if episodes and (episodes[0].phase != 'initial' or episodes[0].policy_id != 'shared'
                         or any(e.phase != 'post_failure' for e in episodes[1:])):
            issues.append('invalid_branch_episode_order')
        if len(episodes) > limit+1 or len({e.episode_id for e in episodes}) != len(episodes):
            issues.append('invalid_episode_count')
        origins, states = [], []
        for episode in episodes:
            try:
                _, raw_bytes, raw = _raw(directory, episode)
                origins.append(_origin(directory, episode, events, cm, raw_bytes))
                state = episode.to_dict()
                for key in ('episode_id', 'policy_id', 'raw_path'):
                    state.pop(key)
                states.append(_digest(dict(episode=state, raw=raw)))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                origins.append(None)
                states.append(None)
                issues.append('unverifiable_episode:' + type(exc).__name__)
        outcomes = _json(directory/'outcomes.json', [])
        outcomes = [o for o in outcomes if o.get('case_id') == cid and o.get('policy_id') == policy_id]
        if len(outcomes) > 1:
            issues.append('duplicate_outcome')
        state = _json(directory/'status.json', {})
        result[cid] = DecisionCase(cid, policy_id, limit+1, str(directory), episodes, decisions,
            tuple(origins), tuple(states), {key: identities.get(cid, {}).get(key) for key in IDENTITY_FIELDS},
            state.get('status', 'not_started'), outcomes[0].get('status') if len(outcomes) == 1 else None,
            tuple(issues), tuple(warnings))
    return result


def _base(cid, left, right):
    return dict(case_id=cid, status=None, first_disagreement=None, compared_decision_boundaries=0,
                left_source=left.source if left else None, right_source=right.source if right else None,
                left_recording_status=left.recording_status if left else 'missing_case',
                right_recording_status=right.recording_status if right else 'missing_case',
                left_outcome_status=left.outcome_status if left else None,
                right_outcome_status=right.outcome_status if right else None)


def _decision(case, n):
    expected = [e.episode_id for e in case.episodes[:n]]
    rows = [d for d in case.decisions if d.get('visible_history_ids') == expected
            and not (str(d.get('decision_id', '')).endswith('/error')
                     and d.get('reason') == 'GENERATION_ERROR')]
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('duplicate_decision_boundary')
    row = rows[0]
    history = History.from_episodes(list(case.episodes[:n]), Budget(case.total_target_budget-1, n-1))
    if (row.get('action') not in ('Retry', 'Revise', 'Stop')
            or not isinstance(row.get('reason'), str)
            or row.get('spent_target_budget') != n-1
            or row.get('remaining_target_budget') != case.total_target_budget-n
            or row.get('current_stage') != history.last.stage
            or row.get('history') != history.summary()):
        raise ValueError('invalid_decision_boundary')
    return dict(action=row['action'], reason=row['reason'], decision_id=row.get('decision_id'),
                source=str(Path(case.source)/'decisions.jsonl'))


def _point(case, n, left, right):
    episode = case.episodes[n-1]
    return dict(after_target_execution=n, stage=episode.stage, trigger=episode.trigger,
                remaining_target_budget=case.total_target_budget-n, left=left, right=right)


def _report(rows, mode, left_label, right_label):
    return dict(mode=mode, left_label=left_label, right_label=right_label,
                actual_group_comparison=mode == 'actual_recorded_arms',
                counterfactual_future_generated=False, causal_effect_estimated=False,
                assigned_cases=len(rows), status_counts=dict(Counter(row['status'] for row in rows)), cases=rows)


def pairwise_first_disagreement(left_cases, right_cases, *, left_label=None, right_label=None):
    """Compare actual recorded actions only along a verified shared execution prefix."""
    rows = []
    for cid in sorted(set(left_cases) | set(right_cases)):
        left, right = left_cases.get(cid), right_cases.get(cid)
        row = _base(cid, left, right)
        rows.append(row)
        if left is None or right is None:
            row['status'] = 'missing_case'
            continue
        if left.issues or right.issues:
            row.update(status='incomparable_invalid_records', left_issues=list(left.issues), right_issues=list(right.issues))
            continue
        if left.identity != right.identity or left.case_id != right.case_id:
            row['status'] = 'incomparable_case_identity'
            continue
        if left.total_target_budget != right.total_target_budget:
            row['status'] = 'incomparable_budget'
            continue
        if not left.episodes or not right.episodes:
            row['status'] = 'missing_history'
            continue
        for n in range(1, min(len(left.episodes), len(right.episodes))+1):
            origin_l = left.execution_origins[n-1] if len(left.execution_origins) >= n else None
            origin_r = right.execution_origins[n-1] if len(right.execution_origins) >= n else None
            state_l = left.state_fingerprints[n-1] if len(left.state_fingerprints) >= n else None
            state_r = right.state_fingerprints[n-1] if len(right.state_fingerprints) >= n else None
            if origin_l is None or origin_r is None or state_l is None or state_r is None:
                row.update(status='incomparable_unverified_execution_origin', after_target_execution=n)
                break
            if origin_l != origin_r or state_l != state_r:
                row.update(status='history_diverged_before_action_disagreement', after_target_execution=n,
                           divergence_reason='independent_target_execution' if origin_l != origin_r else 'trajectory_or_observation_changed')
                break
            try:
                ld, rd = _decision(left, n), _decision(right, n)
            except ValueError as exc:
                row.update(status='incomparable_invalid_decision', reason=str(exc))
                break
            if ld is None or rd is None:
                row.update(status='missing_decision', after_target_execution=n,
                           missing_sides=[side for side, value in (('left', ld), ('right', rd)) if value is None])
                break
            row['compared_decision_boundaries'] += 1
            if ld['action'] != rd['action']:
                row.update(status='first_action_disagreement', first_disagreement=_point(left, n, ld, rd))
                break
            if ld['action'] == 'Stop':
                row.update(status='no_action_disagreement_at_common_stop', after_target_execution=n)
                break
        else:
            row.update(status='incomplete_after_shared_prefix', after_target_execution=min(len(left.episodes), len(right.episodes)))
    return _report(rows, 'actual_recorded_arms', left_label, right_label)


def _policy(spec):
    if hasattr(spec, 'decide'):
        return spec
    config = {ALIASES.get(spec, spec): {}} if isinstance(spec, str) else spec
    if not isinstance(config, dict) or len(config) != 1:
        raise ValueError('Provide a policy object, name, or one-policy configuration')
    return next(iter(build_policies(config).values()))


def replay_first_disagreement(cases, left_policy, right_policy, *, left_label=None, right_label=None):
    """Diagnose two policies on observed history, stopping before off-policy continuations."""
    lp, rp = _policy(left_policy), _policy(right_policy)
    rows = []
    for cid, case in sorted(cases.items()):
        row = _base(cid, case, case)
        row['observed_policy_id'] = case.policy_id
        rows.append(row)
        if case.issues:
            row.update(status='incomparable_invalid_records', issues=list(case.issues))
            continue
        if not case.episodes:
            row['status'] = 'missing_history'
            continue
        for n in range(1, len(case.episodes)+1):
            history = History.from_episodes(list(case.episodes[:n]), Budget(case.total_target_budget-1, n-1))
            ld, rd = lp.decide(history), rp.decide(history)
            left = dict(action=ld.action.value, reason=ld.reason, source='offline_policy_replay')
            right = dict(action=rd.action.value, reason=rd.reason, source='offline_policy_replay')
            row['compared_decision_boundaries'] += 1
            if ld.action != rd.action:
                row.update(status='first_action_disagreement', first_disagreement=_point(case, n, left, right))
                break
            try:
                observed = _decision(case, n)
            except ValueError as exc:
                row.update(status='incomparable_invalid_decision', reason=str(exc))
                break
            if observed is None:
                row.update(status='missing_observed_action', after_target_execution=n)
                break
            if observed['action'] != ld.action.value:
                row.update(status='observed_action_diverged_before_pair_disagreement',
                           after_target_execution=n, boundary=_point(case, n, left, right), observed_action=observed)
                break
            if ld.action.value == 'Stop':
                row.update(status='no_action_disagreement_at_common_stop', after_target_execution=n)
                break
        else:
            row.update(status='incomplete_observed_history', after_target_execution=len(case.episodes))
    return _report(rows, 'replay_on_observed_history', left_label, right_label)
