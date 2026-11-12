"""Offline controller comparison; recorded arms and policy replay remain separate.

Example: --arm B2=runs/example --replay-policies --output-prefix reports/diagnostics
No Target, generation, observation, or pricing API is called.
"""
import argparse
from datetime import datetime, timezone
from itertools import combinations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.summarize_full_evidence import analyze
from src.evaluation.decision_disagreement import (
    load_recorded_cases, pairwise_first_disagreement, replay_first_disagreement,
)
from src.policies.registry import build_policies

ARMS = {'B0': 'always_revise', 'B1': 'stage_aware', 'B2': 'stage_aware_p0_confirm'}


def analyze_arms(arms, *, replay_policies=False):
    if not arms or set(arms) - set(ARMS):
        raise ValueError('expected_one_or_more_B0_B1_B2_arms')
    reports, records = {}, {}
    for label, run in sorted(arms.items()):
        report = analyze(run)
        if report['policy_id'] != ARMS[label]:
            raise ValueError(f'{label}_policy_must_be_{ARMS[label]}')
        reports[label] = dict(source=report['source'], policy_id=report['policy_id'],
            assigned_cases=report['assigned_cases'], status=report['status'],
            total_target_budget=report['total_target_budget'], asr=report['asr'],
            control_diagnostics=report['control_diagnostics'],
            cases=[dict(case_id=c['case_id'], control_diagnostics=c['control_diagnostics'])
                   for c in report['cases']])
        records[label] = load_recorded_cases(run, policy_id=ARMS[label])
    actual = {f'{left}_vs_{right}': pairwise_first_disagreement(
        records[left], records[right], left_label=left, right_label=right)
        for left, right in combinations(sorted(arms), 2)}
    replay = {}
    if replay_policies:
        for source_label, run in sorted(arms.items()):
            manifest = json.loads((Path(run)/'manifest.json').read_text())
            options = manifest['config']['policies'][ARMS[source_label]] or {}
            stage_options = {key: options[key] for key in ('max_retries_per_payload', 'unknown_schedule')
                             if key in options}
            policies = build_policies({'always_revise': {}, 'stage_aware': stage_options,
                                      'stage_aware_p0_confirm': stage_options})
            replay[source_label] = {f'{left}_vs_{right}': replay_first_disagreement(
                records[source_label], policies[ARMS[left]], policies[ARMS[right]],
                left_label=left, right_label=right)
                for left, right in combinations(ARMS, 2)}
    return dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(), api_calls=0,
        arm_mapping=ARMS, arms=reports,
        first_decision_disagreement=dict(
            status='recorded_pairs_available' if actual else 'not_available_single_arm',
            mode='recorded_pairwise', pairs=actual),
        replay_on_observed_history=dict(enabled=replay_policies, by_source_arm=replay,
            note='Offline action diagnostics on saved prefixes, not executed comparison arms or ASR estimates.'),
        limitations=[
            'Independent histories with equal Payload/Gold/Trigger are not a verified common execution prefix.',
            'No decisions or outcomes are inferred after histories/actions diverge.',
            'Retry slots are potential revision opportunities, not measured forgone successful revisions.',
            'Unexecuted revision generation costs are unknown; missing usage and currency remain unknown.',
            'No live comparison experiment is launched by this report.'])


def render_disagreements(result):
    labels = {
        'first_action_disagreement': '首次动作不同',
        'observed_action_diverged_before_pair_disagreement': '两策略一致，但来源动作不同，后续截断',
        'no_action_disagreement_at_common_stop': '兼容前缀至停止，未出现动作分歧',
        'history_diverged_before_action_disagreement': '执行历史先分叉，后续不可比',
    }
    counts = {labels.get(k, k): v for k, v in result['status_counts'].items()}
    lines = ['状态计数：' + json.dumps(counts, ensure_ascii=False), '',
        '| 案例 | 状态 | 第几次执行后 | 阶段 / Trigger | 剩余预算 | 左组动作 | 右组动作 |',
        '|---|---|---:|---|---:|---|---|']
    for row in result['cases']:
        point = row.get('first_disagreement') or row.get('boundary') or {}
        lines.append('| ' + ' | '.join(str(value) for value in (
            row['case_id'], labels.get(row['status'], row['status']), point.get('after_target_execution', row.get('after_target_execution', '—')),
            str(point.get('stage', '—')) + ' / ' + str(point.get('trigger', '—')),
            point.get('remaining_target_budget', '—'),
            point.get('left', {}).get('action', '—'), point.get('right', {}).get('action', '—'))) + ' |')
    lines += ['', '动作原因、记录来源及未完成/不可比原因保存在配套 JSON。', '']
    return lines


def render_markdown(report):
    lines = ['控制策略诊断', '',
        'B0 = AlwaysRevise；B1 = StageAware；B2 = StageAware + P0-confirm。', '',
        '本报告离线读取记录，API 调用为 0。实际组间对照与已观测历史上的策略重放分别报告。', '',
        '| 组 | 案例数 | 官方 Gold ASR | 完整性 |', '|---|---:|---:|---|']
    for label, arm in report['arms'].items():
        asr = arm['asr']
        value = asr['complete_asr']
        rate = '未完整' if value is None else f'{value:.2%}'
        lines.append(f"| {label} | {arm['assigned_cases']} | {asr['confirmed_successes']}/{arm['assigned_cases']}（{rate}） | {arm['status']} |")
    lines += ['', '各组的 Partial Opportunity Rate 与 Revision Opportunity Cost：', '']
    from scripts.summarize_full_evidence import render_control_metrics
    for label, arm in report['arms'].items():
        lines += [f'**{label}**', '', *render_control_metrics(arm['control_diagnostics']), '']
    actual = report['first_decision_disagreement']
    lines += [f"实际 First Decision Disagreement：{actual['status']}。", '']
    # Keep every case's first boundary and its provenance visible. Structured
    # output also retains missing, invalid, and independently diverged cases.
    for pair, result in actual['pairs'].items():
        lines += [f'**实际记录 {pair}**', '', *render_disagreements(result)]
    replay = report['replay_on_observed_history']
    if replay['enabled']:
        lines += ['以下为离线策略重放，不能当作 B0/B1 已执行的对照结果，也不产生反事实 ASR。', '']
        for source, pairs in replay['by_source_arm'].items():
            for pair, result in pairs.items():
                lines += [f'**在 {source} 保存前缀上重放 {pair}**', '', *render_disagreements(result)]
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arm', action='append', required=True, metavar='B2=RUN_PATH')
    parser.add_argument('--replay-policies', action='store_true')
    parser.add_argument('--output-prefix', type=Path, required=True)
    args = parser.parse_args(argv)
    arms = {}
    for item in args.arm:
        label, separator, path = item.partition('=')
        if not separator or not path or label not in ARMS or label in arms:
            parser.error('Each --arm must be a unique B0/B1/B2=RUN_PATH.')
        arms[label] = Path(path)
    report = analyze_arms(arms, replay_policies=args.replay_policies)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    args.output_prefix.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    args.output_prefix.with_suffix('.md').write_text(render_markdown(report))
    print(json.dumps(dict(arms=list(arms), api_calls=0,
        first_decision_disagreement=report['first_decision_disagreement']['status']), ensure_ascii=False))
    return report


if __name__ == '__main__':
    main()
