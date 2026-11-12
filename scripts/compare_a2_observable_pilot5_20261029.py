"""Read-only comparison of A2 and original five-case TFU/controller pilots.

Neither input run is modified. Model usage fields remain separate: provider
input_tokens plus cache tokens is not asserted to be context length or billing.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.analyze_observable_pilot5_20261029 import analyze


def stats(values):
    values = [v for v in values if type(v) in (int, float)]
    return dict(count=len(values), total=sum(values), minimum=min(values),
                maximum=max(values), median=statistics.median(values)) if values else dict(
                    count=0, total=None, minimum=None, maximum=None, median=None)


def initial_payload_fingerprints(run):
    """Read initial payload hashes; episode IDs are reused across independent runs."""
    result = {}
    for path in sorted(Path(run).glob('*/episodes.jsonl')):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get('policy_id') != 'shared' or row.get('phase') != 'initial':
                continue
            cid = row['case_id']
            if cid in result:
                raise ValueError('Duplicate initial episode: ' + cid)
            result[cid] = row.get('payload_sha256') or None
    return result


def outcome_sensitivity(new, old):
    """Observed-cohort bounds, not confidence intervals or treatment effects.

    Unknown terminal outcomes may be success or failure. Keep the union of
    planned tasks in both denominators. Complete-case counts are descriptive:
    equal case IDs do not establish shared initial trajectories or candidates.
    """
    maps = []
    for report in (new, old):
        rows = report['cases']
        by_id = {row['case_id']: row for row in rows}
        if len(by_id) != len(rows):
            raise ValueError('Duplicate comparison case')
        maps.append(by_id)
    ids = sorted(set(maps[0]) | set(maps[1]))
    result = {}
    for pid in sorted(set(new['policies']) | set(old['policies'])):
        values = [[rows.get(cid, {}).get('arms', {}).get(pid, {}).get('gold_success')
                   for cid in ids] for rows in maps]
        if any(v is not None and type(v) is not bool for side in values for v in side):
            raise ValueError('Comparison outcomes must be boolean or null')
        sides = []
        for side in values:
            successes = sum(v is True for v in side)
            unknown = sum(v is None for v in side)
            sides.append(dict(planned_cases=len(ids), confirmed_successes=successes,
                known_failures=sum(v is False for v in side), unknown=unknown,
                success_fraction_bounds=[successes / len(ids), (successes + unknown) / len(ids)]
                    if ids else None))
        a2, original = sides
        a2_bounds, old_bounds = a2['success_fraction_bounds'], original['success_fraction_bounds']
        paired = [(a, o) for a, o in zip(*values) if a is not None and o is not None]
        result[pid] = dict(a2=a2, original=original,
            a2_minus_original_fraction_bounds=[a2_bounds[0] - old_bounds[1],
                a2_bounds[1] - old_bounds[0]] if ids else None,
            both_outcomes_known=len(paired), both_success=sum(a and o for a, o in paired),
            both_failure=sum(not a and not o for a, o in paired),
            a2_only_success=sum(a and not o for a, o in paired),
            original_only_success=sum(o and not a for a, o in paired),
            incomplete_task_pairs=len(ids) - len(paired), causal_effect_identified=False)
    return result


def generation_records(run):
    """Use committed completion events, not possibly incomplete raw files."""
    result = []
    for folder in sorted(Path(run).iterdir()):
        events = folder / 'events.jsonl'
        if not folder.is_dir() or not events.is_file():
            continue
        seen = set()
        for line in events.read_text().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get('event') != 'GENERATION_COMPLETED' or not event.get('raw_path'):
                continue
            path = (folder / event['raw_path']).resolve()
            if not path.is_relative_to(folder.resolve()) or path in seen:
                raise ValueError('Unsafe or duplicate generation completion path')
            seen.add(path)
            result.append(json.loads(path.read_text()))
    return result


def generation_summary(generations):
    groups, reductions = defaultdict(list), []
    for generation in generations:
        for call in generation.get('calls', []):
            groups[call['stage']].append(call)
        a2 = generation.get('routing', {}).get('a2')
        if generation.get('policy_id') == 'shared' and a2:
            attacker = next((c for c in generation.get('calls', []) if c['stage'] == 'attacker_p0'), None)
            original, reduced = a2.get('original_document_chars'), a2.get('a2_document_chars')
            actual = (len(attacker.get('prompt', '')) + len(attacker.get('system_prompt', ''))
                      if attacker else None)
            reductions.append(dict(case_id=generation['case_id'], original_strategy_chars=original,
                a2_strategy_chars=reduced, strategy_reduction_fraction=1-reduced/original if original else None,
                actual_initial_request_chars=actual,
                same_route_original_request_chars=actual + original - reduced
                    if actual is not None and original is not None and reduced is not None else None,
                selected_case_ids={sid: selection['case_ids']
                                   for sid, selection in a2.get('selections', {}).items()}))
    stages = {}
    usage_keys = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')
    for stage, calls in sorted(groups.items()):
        providers = [(c.get('provider') or {}) for c in calls]
        stages[stage] = dict(calls=len(calls), statuses=dict(Counter(c.get('status') for c in calls)),
            errors=dict(Counter(c.get('error_type') or (c.get('provider') or {}).get('error_type')
                                or 'unrecorded' for c in calls if c.get('status') == 'error')),
            prompt_chars=stats(len(c.get('prompt', '')) for c in calls),
            system_chars=stats(len(c.get('system_prompt', '')) for c in calls),
            total_request_chars=stats(len(c.get('prompt', '')) + len(c.get('system_prompt', '')) for c in calls),
            seconds=stats(p.get('seconds') for p in providers),
            successful_call_seconds=stats(p.get('seconds') for c, p in zip(calls, providers)
                                         if c.get('status') == 'completed'),
            usage={key: stats((p.get('usage') or {}).get(key) for p in providers) for key in usage_keys},
            usage_missing_calls=sum(not isinstance(p.get('usage'), dict) for p in providers))
    return dict(stages=stages, a2_initial_prompt_reduction=reductions,
                usage_note='Raw provider fields reported separately; cache semantics and service billing are not independently verified. Missing usage is unknown, not zero.',
                latency_note='Observed call durations include generation and provider/CLI behavior; they do not isolate prompt-length effects.')


def summarize(run):
    report = analyze(run)
    manifest = json.loads((Path(run) / 'manifest.json').read_text())
    config = manifest['config']
    cases = []
    for row in report['cases']:
        cases.append(dict(case_id=row['case_id'], initial=row['initial'], run_status=row['run_status'],
            arms={pid: {key: arm[key] for key in ('gold_success', 'outcome_status', 'actions',
                   'trigger_sequence', 'gold_sequence', 'post_failure_attempts_recorded', 'revise_decisions')}
                  for pid, arm in row['arms'].items()}, issues=row['issues']))
    return dict(run=str(Path(run).resolve()), analysis_status=report['status'],
        parent_status=report['parent_run_status'], policies=report['policies'],
        coverage=report['coverage'], decision_effect=report['decision_effect'],
        outcome_comparison=report['outcome_comparison'], cases=cases, issues=report['issues'],
        cost=report['physical_cost'], generation=generation_summary(generation_records(run)),
        comparison_settings={key: config.get(key) for key in ('backend', 'split', 'dataset', 'goal_specs',
                            'policies', 'observation', 'post_failure_target_budget', 'seed', 'target', 'attacker')})


def compare(new_run, old_run):
    new, old = summarize(new_run), summarize(old_run)
    settings = new['comparison_settings']
    matches = {key: value == old['comparison_settings'].get(key) for key, value in settings.items()
               if key != 'attacker'}
    matches.update({'attacker_' + key: settings['attacker'].get(key) ==
                    old['comparison_settings']['attacker'].get(key) for key in ('model', 'effort', 'timeout')})
    manifests = [json.loads((Path(path) / 'manifest.json').read_text()) for path in (new_run, old_run)]
    public_fields = ('case_id', 'base_task_id', 'suite_name', 'user_task_id', 'injection_task_id',
                     'user_task_prompt', 'injection_task_goal', 'context_with_placeholder',
                     'injection_candidates', 'goal_spec', 'resource_grants')
    case_maps = [{c['case_id']: {key: c.get(key) for key in public_fields}
                  for c in manifest.get('cases', [])} for manifest in manifests]
    case_matches = {cid: cid in case_maps[0] and cid in case_maps[1]
                        and case_maps[0][cid] == case_maps[1][cid]
                    for cid in sorted(set(case_maps[0]) | set(case_maps[1]))}
    frozen = [manifest.get('inputs_sha256', {}) for manifest in manifests]
    source_paths = {path for paths in frozen for path in paths
                    if path.startswith('strategy_library/') and path.endswith('.md')}
    source_paths |= {settings.get('dataset'), settings.get('goal_specs')}
    input_matches = {path: path in frozen[0] and path in frozen[1] and frozen[0][path] == frozen[1][path]
                     for path in sorted(path for path in source_paths if path)}
    first = {'old': old['cases'], 'a2': new['cases']}
    initial = {name: dict(successes=sum(r['initial'] is not None and r['initial']['gold'] is True for r in rows),
                          valid_failures=sum(r['initial'] is not None and r['initial']['gold'] is False for r in rows),
                          unknown_or_missing=sum(r['initial'] is None or r['initial']['gold'] is None for r in rows))
               for name, rows in first.items()}
    fingerprints = [initial_payload_fingerprints(path) for path in (new_run, old_run)]
    initial_matches = {cid: dict(a2_sha256=fingerprints[0].get(cid),
        original_sha256=fingerprints[1].get(cid),
        same_payload=(fingerprints[0][cid] == fingerprints[1][cid])
            if fingerprints[0].get(cid) and fingerprints[1].get(cid) else None)
        for cid in sorted(set(case_maps[0]) | set(case_maps[1]))}
    return dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(),
        status='complete' if new['analysis_status'] == old['analysis_status'] == 'complete' else 'partial_or_audit_issues',
        setting_matches=matches, public_case_matches=case_matches, frozen_input_matches=input_matches,
        initial_outcomes=initial, a2=new, original=old,
        initial_payload_comparison=initial_matches,
        outcome_sensitivity=outcome_sensitivity(new, old),
        limitations=[
            'Each run shares its own new P0/H1 across its two arms. Old versus A2 does not share P0/H1 or generated candidates.',
            'Budget is one shared initial execution plus at most three further Target executions per arm; this is not total ASR@3.',
            'End-to-end confirmed fractions retain all five planned cases. Post-failure fractions use only that run\'s valid initial failures; denominators can differ.',
            'Unknown/error outcomes are not ordinary failures; complete success fractions remain null when an arm is incomplete.',
            'Five previously exposed tasks and one stochastic path per arm do not establish causal compression benefit or non-inferiority.',
            'Within-run masked shadows compare decisions on observed histories only; no counterfactual reward is observed.',
            'Preflight calls are separate from scored-run generation and target statistics here.',
            'Missing-outcome bounds concern only these observed tasks; they are not confidence intervals, generalization estimates, or causal effects.',
            'Matching payload hashes establish identical text only, not shared H1 execution or provider state.',
        ])


def fmt(value):
    if value is None:
        return '未知'
    if isinstance(value, float):
        return f'{value:.1f}'
    return str(value)


def markdown(report):
    lines = ['# A2 与原文策略库：五例 TFU / Retry–Revise 重跑', '',
        f"报告状态：`{report['status']}`。每个运行内部共享新生成的 P0/H1，随后每臂最多三次执行（总计最多四次）。", '',
        '两个运行之间没有共享首轮或候选；以下是描述性比较。首轮成功计入端到端结果，不进入首轮失败后的决策分母。', '',
        '| 运行 | 臂 | 确认成功 / 预定任务 | 未知或错误 | 首轮失败后成功 / 可决策任务 |',
        '|---|---|---|---|---|']
    for key, label in [('original', '原文'), ('a2', 'A2')]:
        for pid, row in report[key]['policies'].items():
            lines.append(f"| {label} | {pid} | {row['confirmed_successes']}/{row['assigned_cases']} | "
                         f"{row['unknown_or_error_outcomes']} | {row['post_failure_successes']}/{row['first_failure_cases']} |")
    lines += ['', '## 未决结果与首轮可比性', '',
        '以下范围仅把已测任务中的未决结果分别视为失败或成功；不是置信区间，也不估计总体效果或因果差异。', '',
        '| 臂 | 原文成功比例范围 | A2 成功比例范围 | A2 − 原文范围（百分点） | 两版均有确定结果 | 原文独有成功 / A2 独有成功 |',
        '|---|---|---|---|---|---|']
    def interval(bounds, unit='%'):
        return '未知' if bounds is None else f'{100 * bounds[0]:.1f}{unit} 至 {100 * bounds[1]:.1f}{unit}'
    for pid, row in report['outcome_sensitivity'].items():
        lines.append(f"| {pid} | {interval(row['original']['success_fraction_bounds'])} | "
                     f"{interval(row['a2']['success_fraction_bounds'])} | "
                     f"{interval(row['a2_minus_original_fraction_bounds'], '')} | {row['both_outcomes_known']} | "
                     f"{row['original_only_success']} / {row['a2_only_success']} |")
    matches = list(report['initial_payload_comparison'].values())
    lines += ['', f"P0 字节哈希：相同 {sum(r['same_payload'] is True for r in matches)} 例，"
              f"不同 {sum(r['same_payload'] is False for r in matches)} 例，"
              f"未知 {sum(r['same_payload'] is None for r in matches)} 例。"
              '案例 ID / episode ID 相同不表示共享执行；即使 P0 相同，也不能据此认定 H1 相同。']
    lines += ['', '| 运行 | 有效执行 T/F/U | 证据改变动作 / 已重放决策 | Target 记录 |', '|---|---|---|---|']
    for key, label in [('original', '原文'), ('a2', 'A2')]:
        row = report[key]
        lines.append(f"| {label} | {row['coverage']['trigger_counts']} | "
                     f"{row['decision_effect']['evidence_changes_action']}/{row['decision_effect']['checked_stage_aware_decisions']} | "
                     f"{row['cost']['target_results_recorded']} |")
    lines += ['', '## A2 逐例路径', '', '| 样本 | 首轮 Gold / Trigger | 臂 | 动作 | Trigger 序列 | 最终成功 |',
              '|---|---|---|---|---|---|']
    for row in report['a2']['cases']:
        initial = row['initial']
        initial_text = f"{initial['gold']} / {initial['trigger']}" if initial else '缺失'
        for pid, arm in row['arms'].items():
            actions = ' → '.join(a['action'] for a in arm['actions']) or '无后续动作'
            triggers = ' → '.join(arm['trigger_sequence']) or '无记录'
            lines.append(f"| {row['case_id']} | {initial_text} | {pid} | {actions} | {triggers} | {fmt(arm['gold_success'])} |")
    lines += ['', '## 已记录生成调用', '',
              '输入、缓存读取、输出均为服务端原样报告字段；输入与缓存不相加作为上下文长度或账单。耗时不单独归因于输入长度。', '',
              '| 运行 / 阶段 | 调用数 | 请求字符中位数 | input_tokens 中位数 | cache_read 中位数 | 耗时中位数（秒） | 缺 usage 调用 |',
              '|---|---|---|---|---|---|---|']
    for key, label in [('original', '原文'), ('a2', 'A2')]:
        for stage, row in report[key]['generation']['stages'].items():
            lines.append(f"| {label} / {stage} | {row['calls']} | {fmt(row['total_request_chars']['median'])} | "
                         f"{fmt(row['usage']['input_tokens']['median'])} | {fmt(row['usage']['cache_read_input_tokens']['median'])} | "
                         f"{fmt(row['seconds']['median'])} | {row['usage_missing_calls']} |")
    lines += ['', '## A2 同一路由的确定性文本缩减', '',
              '| 样本 | 原策略正文字符 | A2 策略正文字符 | 减少 |', '|---|---|---|---|']
    for row in report['a2']['generation']['a2_initial_prompt_reduction']:
        reduction = row['strategy_reduction_fraction']
        lines.append(f"| {row['case_id']} | {row['original_strategy_chars']} | {row['a2_strategy_chars']} | "
                     f"{100 * reduction:.1f}% |" if reduction is not None else f"| {row['case_id']} | 未知 | 未知 | 未知 |")
    lines += ['', '## 解释边界', ''] + ['- ' + line for line in report['limitations']]
    lines += ['', '公开任务、目标规格与观察授权一致性：`' + json.dumps(report['public_case_matches']) + '`。',
              '冻结的数据与原始策略文件一致：' + str(all(report['frozen_input_matches'].values())) + '（逐文件哈希见 JSON）。',
              '配置一致性（排除 A2 表示方式及资源路径）：`' + json.dumps(report['setting_matches'], ensure_ascii=False) + '`。', '',
              f"原文运行：[记录]({report['original']['run']})；A2 运行：[记录]({report['a2']['run']})。", '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--old-run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.run, args.old_run)
    prefix = args.output.with_suffix('') if args.output.suffix in ('.json', '.md') else args.output
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    prefix.with_suffix('.md').write_text(markdown(report))
    print(json.dumps(dict(status=report['status'], output=str(prefix.resolve()),
                         initial_outcomes=report['initial_outcomes']), ensure_ascii=False))


if __name__ == '__main__':
    main()
