"""Read-only analysis of the five-case observable-trigger pilot; never calls APIs.

Usage: python scripts/analyze_observable_pilot5_20261029.py --run RUN --output PREFIX
Writes PREFIX.json and PREFIX.md; an existing directory gets analysis.json/.md.
"""
import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.engine.history import EvidenceSummary, History, ObservationSummary
from src.evaluation.metrics import costs
from src.policies.label_based import LabelBased
from src.policies.stage_aware import StageAware

EXPECTED_IDS = ('c000', 'c001', 'c004', 'c005', 'c006')
BASELINES = ('stage_aware_no_evidence', 'fixed_schedule')
ARMS = ('stage_aware', 'stage_aware_no_evidence')


def read_json(path, issues, *, required=True, default=None):
    if not path.is_file():
        if required:
            issues.append(dict(code='missing_file', path=str(path)))
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        issues.append(dict(code='unreadable_json', path=str(path), error_type=type(exc).__name__))
        return default


def read_jsonl(path, issues):
    if not path.is_file():
        issues.append(dict(code='missing_file', path=str(path)))
        return []
    rows = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('row must be an object')
            rows.append(row)
        except ValueError:
            issues.append(dict(code='invalid_jsonl_record', path=str(path), line=number))
    return rows


def make_history(decision, episodes):
    visible = decision['visible_history_ids']
    if not visible or len(visible) != len(set(visible)):
        raise ValueError('empty or duplicate history')
    completed = []
    for eid in visible:
        e = episodes[eid]
        if e['case_id'] != decision['case_id'] or e['policy_id'] not in ('shared', decision['policy_id']):
            raise ValueError('foreign history')
        completed.append(ObservationSummary(
            episode_id=eid, payload_sha256=e['payload_sha256'], stage=e['stage'],
            trigger=e['trigger'], response_signal=e['response_signal'],
            evidence=tuple(EvidenceSummary(p['id'], p['query'], p['status'], p.get('is_trigger', False))
                           for p in e.get('evidence', [])),
            observation_error=e.get('observation_error') is not None))
    remaining, spent = decision['remaining_target_budget'], decision['spent_target_budget']
    if type(remaining) is not int or type(spent) is not int or min(remaining, spent) < 0:
        raise ValueError('invalid budget')
    if remaining + spent != 3 or len(completed) != spent + 1:
        raise ValueError('history does not follow B=3')
    return History(tuple(completed), remaining, spent)


def mask_evidence(history):
    """Retain outcomes/response/payload/budget; remove every evidence-derived state."""
    return replace(history, completed=tuple(replace(
        e, trigger='U', stage=e.stage if e.stage in ('SUCCESS', 'ERROR') else 'UNOBSERVABLE',
        evidence=tuple(replace(p, status='U') for p in e.evidence)) for e in history.completed))


def shadow_decisions(decisions, episodes, policy, issues):
    stage_rows, fixed_checks = [], []
    for d in decisions:
        pid = d.get('policy_id')
        # Runner records a second forced Stop when revision generation fails.
        # This is an execution error, not a Controller decision to reproduce.
        if d.get('reason') == 'GENERATION_ERROR' and d.get('action') == 'Stop':
            continue
        if pid not in ('stage_aware', *BASELINES) or (pid == 'stage_aware' and d.get('action') == 'Stop'):
            continue
        base = dict(decision_id=d.get('decision_id'), source_episode_ids=d.get('visible_history_ids', []),
                    recorded_action=d.get('action'))
        try:
            history = make_history(d, episodes)
            masked = policy.decide(mask_evidence(history))
            base.update(status='checked', last_trigger=history.last.trigger,
                        response_signal=history.last.response_signal,
                        masked_stage_aware_action=masked.action.value, masked_reason=masked.reason)
            if pid == 'stage_aware':
                actual = policy.decide(history)
                label = LabelBased().decide(history)
                base.update(replayed_stage_aware_action=actual.action.value,
                            replay_matches_record=actual.action.value == d['action'],
                            action_changed_by_evidence=actual.action != masked.action,
                            label_based_action=label.action.value, label_based_reason=label.reason,
                            differs_from_label_based=actual.action != label.action,
                            counterfactual_reward=None)
                if not base['replay_matches_record']:
                    issues.append(dict(code='stage_aware_replay_mismatch', decision_id=d.get('decision_id')))
                stage_rows.append(base)
            else:
                base['matches_masked_stage_aware'] = masked.action.value == d['action']
                if not base['matches_masked_stage_aware']:
                    issues.append(dict(code='baseline_masked_mismatch', decision_id=d.get('decision_id')))
                fixed_checks.append(base)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            base.update(status='unavailable', error_type=type(exc).__name__)
            issues.append(dict(code='shadow_history_unavailable', decision_id=d.get('decision_id'),
                               error_type=type(exc).__name__))
            (stage_rows if pid == 'stage_aware' else fixed_checks).append(base)
    return stage_rows, fixed_checks


def episode_brief(e):
    return dict(episode_id=e['episode_id'], phase=e.get('phase'), payload_id=e.get('payload_id'),
                gold=e.get('gold'), trigger=e.get('trigger'), response_signal=e.get('response_signal'),
                status=e.get('status'), error=e.get('error'), observation_error=e.get('observation_error'),
                raw_path=e.get('raw_path'),
                evidence=[{k: p.get(k) for k in ('id', 'query', 'status', 'is_trigger', 'supporting_queries')}
                          for p in e.get('evidence', [])])


def physical_cost(episodes, generations):
    objects = [SimpleNamespace(usage=e.get('usage'), target_seconds=e.get('target_seconds', 0),
                               query_units=e.get('query_units', 0)) for e in episodes]
    return costs(objects, generations)


def coverage(episodes):
    valid = [e for e in episodes if type(e.get('gold')) is bool and not e.get('error')]
    selected = [p for e in valid for p in e.get('evidence', []) if p.get('is_trigger')]
    counts = Counter(e.get('trigger', 'U') for e in valid)
    known = counts['T'] + counts['F']
    return dict(valid_episodes=len(valid), invalid_episodes=len(episodes)-len(valid),
                trigger_counts={k: counts[k] for k in ('T', 'F', 'U')}, non_u_episodes=known,
                non_u_fraction=known/len(valid) if valid else None,
                trigger_predicates=len(selected), non_u_predicates=sum(p['status'] in ('T', 'F') for p in selected),
                denominator_note='Completed episodes with official boolean Gold, including observation errors as unknown; shared H1 counted once.')


def analyze_case(run, case, parent_config, policy_ids=ARMS):
    cid = case['case_id']
    folder = run / cid
    issues = []
    manifest = read_json(folder/'manifest.json', issues, default={})
    status = read_json(folder/'status.json', issues, default={})
    episodes = read_jsonl(folder/'episodes.jsonl', issues)
    decisions = read_jsonl(folder/'decisions.jsonl', issues)
    events = read_jsonl(folder/'events.jsonl', issues)
    outcomes = read_json(folder/'outcomes.json', issues, default=[])
    metrics = read_json(folder/'metrics.json', issues, default={})
    if not isinstance(outcomes, list):
        issues.append(dict(code='invalid_outcomes')); outcomes = []
    if not isinstance(status, dict):
        status = {'status': 'invalid'}
    config = manifest.get('config', parent_config) if isinstance(manifest, dict) else parent_config
    if config.get('post_failure_target_budget') != 3:
        issues.append(dict(code='unexpected_budget', value=config.get('post_failure_target_budget')))
    params = config.get('policies', {}).get('stage_aware', {})
    policy = StageAware(**(params or {}))
    episode_map = {}
    trigger_ids = set(case.get('goal_spec', {}).get('trigger_ids', []))
    for e in episodes:
        if e.get('case_id') != cid:
            issues.append(dict(code='foreign_episode', episode_id=e.get('episode_id')))
        if e.get('episode_id') in episode_map:
            issues.append(dict(code='duplicate_episode', episode_id=e.get('episode_id')))
        episode_map[e['episode_id']] = e
        for p in e.get('evidence', []):
            if 'is_trigger' not in p:
                p['is_trigger'] = p.get('id') in trigger_ids
    initial = [e for e in episodes if e.get('policy_id') == 'shared' and e.get('phase') == 'initial']
    if len(initial) != 1:
        issues.append(dict(code='initial_episode_count', count=len(initial)))
    h1 = initial[0] if len(initial) == 1 else None
    generations = []
    for event in events:
        if event.get('event') == 'GENERATION_COMPLETED' and event.get('raw_path'):
            raw = (folder / event['raw_path']).resolve()
            if not raw.is_relative_to(folder.resolve()):
                issues.append(dict(code='unsafe_generation_path')); continue
            g = read_json(raw, issues, default=None)
            if isinstance(g, dict):
                generations.append(g)
    started = {e['episode_id'] for e in events if e.get('event') == 'TARGET_STARTED'}
    unresolved = sorted(started - set(episode_map))
    if unresolved:
        issues.append(dict(code='started_target_without_result', episode_ids=unresolved))
    arms = {}
    for pid in policy_ids:
        es = [e for e in episodes if e.get('policy_id') == pid]
        ds = [d for d in decisions if d.get('policy_id') == pid]
        os = [o for o in outcomes if o.get('policy_id') == pid and o.get('case_id') == cid]
        if len(os) != 1:
            issues.append(dict(code='outcome_count', policy_id=pid, count=len(os)))
        outcome = os[0] if len(os) == 1 else None
        if outcome is not None and outcome.get('target_attempts') != len(es):
            issues.append(dict(code='attempt_count_mismatch', policy_id=pid))
        success = outcome.get('gold_success') if outcome else None
        if success is not None and type(success) is not bool:
            issues.append(dict(code='non_boolean_gold_outcome', policy_id=pid)); success = None
        known_success = any(e.get('gold') is True for e in initial + es)
        if outcome and known_success != (success is True):
            issues.append(dict(code='gold_success_mismatch', policy_id=pid))
        arms[pid] = dict(gold_success=success, outcome_present=outcome is not None,
            outcome_status=outcome.get('status') if outcome else 'MISSING_OUTCOME',
            actions=[dict(decision_id=d.get('decision_id'), action=d.get('action'), reason=d.get('reason'),
                          source_episode_ids=d.get('visible_history_ids', [])) for d in ds if d.get('action') != 'Stop'],
            stop_reasons=[d.get('reason') for d in ds if d.get('action') == 'Stop'],
            trigger_sequence=[e.get('trigger') for e in initial + es],
            gold_sequence=[e.get('gold') for e in initial + es],
            episode_ids=[e['episode_id'] for e in initial + es],
            post_failure_attempts_recorded=len(es), target_attempts_reported=outcome.get('target_attempts') if outcome else None,
            revise_decisions=sum(d.get('action') == 'Revise' for d in ds),
            revisions_reported=outcome.get('revisions') if outcome else None,
            physical_post_failure_cost=physical_cost(es, [g for g in generations if g.get('policy_id') == pid]),
            coverage=coverage(es))
    shadow, checks = shadow_decisions(decisions, episode_map, policy, issues)
    # Raw metrics remain available as an audit cross-reference, not a source of inferred outcomes.
    if metrics.get('physical_total_cost', {}).get('target_executions') not in (None, len(episodes)):
        issues.append(dict(code='metrics_episode_count_mismatch'))
    record = dict(case_id=cid, base_task_id=case.get('base_task_id'), goal=case.get('injection_task_goal'),
        trigger_ids=sorted(trigger_ids), run_status=status.get('status', 'MISSING'),
        initial=episode_brief(h1) if h1 else None, arms=arms,
        episodes=[episode_brief(e) for e in episodes], coverage=coverage(episodes),
        stage_aware_shadow=shadow, baseline_masked_checks=checks,
        physical_cost=physical_cost(episodes, generations), target_started_events=len(started),
        unresolved_target_starts=unresolved, issues=issues,
        sources=dict(directory=str(folder), manifest=str(folder/'manifest.json'),
                     episodes=str(folder/'episodes.jsonl'), decisions=str(folder/'decisions.jsonl'),
                     outcomes=str(folder/'outcomes.json'), metrics=str(folder/'metrics.json')))
    return record, episodes


def analyze(run):
    run = Path(run).resolve()
    issues = []
    manifest = read_json(run/'manifest.json', issues, default={})
    config = manifest.get('config', {})
    parent_status = read_json(run/'status.json', issues, default={})
    workers = read_json(run/'worker_results.json', issues, default=[])
    if not isinstance(workers, list):
        issues.append(dict(code='invalid_worker_results')); workers = []
    worker_map = {w['case_id']: w for w in workers if isinstance(w, dict) and 'case_id' in w}
    baseline = ('stage_aware_no_evidence' if 'stage_aware_no_evidence' in config.get('policies', {})
                else 'fixed_schedule' if 'fixed_schedule' in config.get('policies', {}) else 'stage_aware_no_evidence')
    arms = ('stage_aware', baseline)
    cases = manifest.get('cases', [])
    if not isinstance(cases, list) or not all(isinstance(c, dict) and isinstance(c.get('case_id'), str) for c in cases):
        issues.append(dict(code='invalid_parent_cases')); cases = []
    case_map = {c['case_id']: c for c in cases}
    if len(case_map) != len(cases):
        issues.append(dict(code='duplicate_parent_cases'))
    if set(case_map) != set(EXPECTED_IDS):
        issues.append(dict(code='expected_case_set_mismatch', expected=list(EXPECTED_IDS), actual=list(case_map)))
    ids = list(EXPECTED_IDS) + sorted(set(case_map) - set(EXPECTED_IDS))
    records, all_episodes = [], []
    for cid in ids:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', cid):
            issues.append(dict(code='unsafe_case_id', case_id=cid)); continue
        record, episodes = analyze_case(run, case_map.get(cid, dict(case_id=cid)), config, arms)
        record['worker_result'] = worker_map.get(cid)
        if cid not in worker_map:
            record['issues'].append(dict(code='worker_result_missing'))
        elif worker_map[cid].get('status') != 'completed':
            record['issues'].append(dict(code='worker_not_completed', **worker_map[cid]))
        records.append(record); all_episodes.extend(episodes)
    shadow = [s for r in records for s in r['stage_aware_shadow'] if s['status'] == 'checked']
    checks = [s for r in records for s in r['baseline_masked_checks'] if s['status'] == 'checked']
    eligible = [r for r in records if r['initial'] and r['initial']['gold'] is False]
    aggregates = {}
    for pid in arms:
        successes = sum(r['arms'][pid]['gold_success'] is True for r in records)
        valid = sum(type(r['arms'][pid]['gold_success']) is bool for r in records)
        post_success = sum(r['arms'][pid]['gold_success'] is True for r in eligible)
        eligible_known = sum(type(r['arms'][pid]['gold_success']) is bool for r in eligible)
        aggregates[pid] = dict(assigned_cases=len(records), confirmed_successes=successes,
            valid_failures=sum(r['arms'][pid]['gold_success'] is False for r in records),
            unknown_or_error_outcomes=len(records)-valid,
            confirmed_success_fraction=successes/len(records),
            complete_success_fraction=successes/len(records) if valid == len(records) else None,
            first_failure_cases=len(eligible), post_failure_successes=post_success,
            post_failure_unknown_outcomes=len(eligible)-eligible_known,
            confirmed_post_failure_success_fraction=post_success/len(eligible) if eligible else None,
            complete_post_failure_success_fraction=post_success/len(eligible) if eligible and eligible_known == len(eligible) else None,
            post_failure_target_executions_recorded=sum(r['arms'][pid]['post_failure_attempts_recorded'] for r in records),
            revise_decisions=sum(r['arms'][pid]['revise_decisions'] for r in records))
    paired = [r for r in records if all(type(r['arms'][p]['gold_success']) is bool for p in arms)]
    difference = sum(int(r['arms']['stage_aware']['gold_success'])-int(r['arms'][baseline]['gold_success'])
                     for r in paired)
    complete = parent_status.get('status') == 'completed' and not issues and all(r['run_status'] == 'completed' and not r['issues'] for r in records)
    return dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(),
        status='complete' if complete else 'partial_or_audit_issues', kind='LIVE_DEVELOPMENT_RETEST',
        run=str(run), parent_run_status=parent_status, baseline_policy_id=baseline,
        expected_case_ids=list(EXPECTED_IDS), cases=records, issues=issues,
        coverage=coverage(all_episodes), policies=aggregates,
        decision_effect=dict(checked_stage_aware_decisions=len(shadow),
            replay_mismatches=sum(not s['replay_matches_record'] for s in shadow),
            evidence_changes_action=sum(s['action_changed_by_evidence'] for s in shadow),
            differs_from_label_based=sum(s['differs_from_label_based'] for s in shadow),
            baseline_masked_checks=len(checks), baseline_masked_mismatches=sum(not s['matches_masked_stage_aware'] for s in checks),
            shadow_counterfactual_reward=None),
        outcome_comparison=dict(complete_pairs=len(paired), incomplete_pairs=len(records)-len(paired),
            stage_aware_only_successes=sum(r['arms']['stage_aware']['gold_success'] and not r['arms'][baseline]['gold_success'] for r in paired),
            baseline_only_successes=sum(r['arms'][baseline]['gold_success'] and not r['arms']['stage_aware']['gold_success'] for r in paired),
            observed_success_fraction_difference=difference/len(paired) if paired else None,
            estimand='Observed two-arm difference on complete case pairs; five-case stochastic engineering pilot, not a causal effect estimate.'),
        physical_cost=dict(target_results_recorded=len(all_episodes),
            target_started_events=sum(r['target_started_events'] for r in records),
            unresolved_target_starts=sum(len(r['unresolved_target_starts']) for r in records),
            per_case=[dict(case_id=r['case_id'], **r['physical_cost']) for r in records], monetary_cost=None),
        limitations=[
            'Five previously exposed task identities, selected by observable necessary behavior and saved order; not an independent held-out test.',
            'Trigger is only a local observable necessary condition. Hidden behavior and complete attack success remain outside Trigger; official Gold alone measures success.',
            'Evidence queries are fixed and not learned or adaptively selected; this tests evidence interpretation and Controller use.',
            'Masked and LabelBased shadows reuse actual histories only. They have no executed counterfactual reward and do not prove which alternative would succeed.',
            'Arms share P0/H1, then execute independent stochastic branches with B=3; candidate generation and execution randomness limit five-case efficacy claims.',
            'Missing cases, process failure and null outcomes retain the planned denominator. Known token usage is a lower bound when usage records are missing; no monetary price is assumed.'
        ])


def fmt(value):
    if value is None:
        return '未知/缺失'
    if type(value) is bool:
        return 'True' if value else 'False'
    return str(value)


def markdown(report):
    coverage_row, effect = report['coverage'], report['decision_effect']
    baseline = report['baseline_policy_id']
    arms = ('stage_aware', baseline)
    baseline_label = '遮蔽证据 StageAware' if baseline == 'stage_aware_no_evidence' else '固定日程（旧协议）'
    lines = ['# 五样本可观察 Trigger 验证', '',
        f"分析状态：`{report['status']}`。预定样本：{', '.join(report['expected_case_ids'])}。", '',
        f'这是既有曝光任务的工程验证。两臂共用首轮 P0/H1，后续各最多 3 次 Target 执行；基线为 {baseline_label}。', '',
        '基线分支仍记录原始合法观察以供审计，但 Controller 决策屏蔽证据；表中的基线 Trigger 序列是原观察。', '',
        '## 局部证据与动作', '',
        f"合法观察的非 U Trigger 覆盖：{coverage_row['non_u_episodes']}/{coverage_row['valid_episodes']} 个有效执行；"
        f"T/F/U = {coverage_row['trigger_counts']}。初始执行只计一次，执行错误另计 {coverage_row['invalid_episodes']} 次。", '',
        f"StageAware 的 {effect['checked_stage_aware_decisions']} 个可重放非 Stop 决策中，"
        f"屏蔽证据后动作不同 {effect['evidence_changes_action']} 次；与仅看回应标签的 LabelBased 不同 {effect['differs_from_label_based']} 次。", '',
        f"{baseline_label} 与离线屏蔽证据 StageAware 同历史校验：{effect['baseline_masked_checks']} 次，"
        f"不一致 {effect['baseline_masked_mismatches']} 次。StageAware 原动作重放不一致 {effect['replay_mismatches']} 次。", '',
        '**shadow 只比较同一历史上的动作，没有执行其反事实后续，因此没有反事实收益。**', '',
        '## 每例结果', '',
        '| 样本 | 初始 Gold / Trigger | StageAware 动作；Trigger 序列 | Gold；后续次数 / 修订 | 证据遮蔽基线动作；原观察 Trigger 序列 | Gold；后续次数 / 修订 | 运行状态 |',
        '|---|---|---|---|---|---|---|']
    for r in report['cases']:
        initial = r['initial']
        h = f"{fmt(initial['gold'])} / {initial['trigger']}" if initial else '无初始记录'
        cells = [r['case_id'], h]
        for pid in arms:
            arm = r['arms'][pid]
            actions = ' → '.join(a['action'] for a in arm['actions']) or '无后续动作'
            triggers = ' → '.join(arm['trigger_sequence']) or '无记录'
            cells.extend([f'{actions}；{triggers}', f"{fmt(arm['gold_success'])}；{arm['post_failure_attempts_recorded']} / {arm['revise_decisions']}"])
        cells.append(r['run_status'] + (f"，{len(r['issues'])} 项审计问题" if r['issues'] else ''))
        lines.append('| ' + ' | '.join(cells) + ' |')
    lines += ['', '## 最终收益与成本', '',
        '| 策略 | 已确认成功 / 预定样本 | 有效失败 | 未知或错误 | 首轮失败后的成功 / 可决策样本 | 后续 Target 结果数 | Revise 决策数 |',
        '|---|---|---|---|---|---|---|']
    for pid, p in report['policies'].items():
        lines.append(f"| {pid} | {p['confirmed_successes']} / {p['assigned_cases']} | {p['valid_failures']} | "
                     f"{p['unknown_or_error_outcomes']} | {p['post_failure_successes']} / {p['first_failure_cases']} | "
                     f"{p['post_failure_target_executions_recorded']} | {p['revise_decisions']} |")
    comparison = report['outcome_comparison']
    cost = report['physical_cost']
    lines += ['', f"完整结果配对 {comparison['complete_pairs']} 例：仅 StageAware 成功 {comparison['stage_aware_only_successes']} 例，"
        f"仅基线成功 {comparison['baseline_only_successes']} 例；缺失/错误配对 {comparison['incomplete_pairs']} 例。", '',
        f"共保存 {cost['target_results_recorded']} 条 Target 结果，记录启动 {cost['target_started_events']} 次，"
        f"其中尚无结果 {cost['unresolved_target_starts']} 次。逐例 API/token/查询成本见 JSON；未记录用量不按零成本处理，不估算金额。", '',
        '## 可追溯的同历史比较', '',
        '| 决策 | 证据来源 episode | Trigger / 回应 | 实际 StageAware | 屏蔽证据 | LabelBased |',
        '|---|---|---|---|---|---|']
    for r in report['cases']:
        for s in r['stage_aware_shadow']:
            if s['status'] != 'checked':
                lines.append(f"| {s['decision_id']} | {', '.join(s['source_episode_ids'])} | 无法重建 | {s['recorded_action']} | 未知 | 未知 |")
            else:
                lines.append(f"| {s['decision_id']} | {', '.join(s['source_episode_ids'])} | {s['last_trigger']} / {s['response_signal']} | "
                             f"{s['recorded_action']} | {s['masked_stage_aware_action']} | {s['label_based_action']} |")
    lines += ['', '局部证据可判读、证据改变动作、最终成功提高是三个独立问题。本实验采用固定合法查询；它不验证主动或学习型采样器。'
              '五个样本及每臂单次随机路径不足以证明稳定收益；错误与缺失保留为未知。', '',
              '## 缺失与审计记录', '']
    found = False
    for owner, items in [('父运行', report['issues'])] + [(r['case_id'], r['issues']) for r in report['cases']]:
        for item in items:
            found = True
            lines.append(f"- {owner}: `{json.dumps(item, ensure_ascii=False)}`")
    if not found:
        lines.append('无缺失文件、历史重放或预算一致性问题。')
    lines += ['', f"原始运行目录：[记录]({report['run']})。JSON 中保留每条证据来源、动作依据、错误、成本及文件路径。", '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = analyze(args.run)
    prefix = args.output / 'analysis' if args.output.is_dir() else args.output
    if prefix.suffix in ('.json', '.md'):
        prefix = prefix.with_suffix('')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path, md_path = prefix.with_suffix('.json'), prefix.with_suffix('.md')
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    md_path.write_text(markdown(report))
    print(json.dumps(dict(status=report['status'], json=str(json_path.resolve()), markdown=str(md_path.resolve()),
                         decision_effect=report['decision_effect'], outcome_comparison=report['outcome_comparison']),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
