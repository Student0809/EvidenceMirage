#!/usr/bin/env python3
"""Summarize the fixed-H1 pilot, preserving errors and case-level pairing."""
import argparse
from itertools import product
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_revise_ablation_20261030 import ARMS, REPETITIONS, summarize
from src.engine.storage import write_json

LABELS = {'full_original': '完整策略库', 'legacy_a2': '旧 A2',
          'a2_v2': '新检索 A2 v2', 'a2_v2_calibrated': '新检索＋反馈校准'}


def bootstrap_case_interval(differences):
    """Exact enumeration of the empirical case bootstrap for this five-case pilot."""
    if not differences:
        return None
    if len(differences) > 5:
        raise ValueError('This exact pilot bootstrap supports at most five case pairs')
    values = sorted(statistics.mean(xs) for xs in product(differences, repeat=len(differences)))
    def percentile(p):
        index = (len(values) - 1) * p
        lower = int(index)
        return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower)
    return [percentile(.025), percentile(.975)]


def analyze(run):
    manifest = json.loads((run / 'manifest.json').read_text())
    rows = json.loads((run / 'results.json').read_text())
    report = summarize(rows, manifest['case_ids'])
    report['run'] = str(run.resolve())
    report['run_status'] = json.loads((run / 'status.json').read_text())['status']
    report['source_run'] = manifest['source_run']
    amendment_path = run / 'parallel_handoff_20261030/manifest.json'
    report['scheduling_amendment'] = json.loads(amendment_path.read_text()) if amendment_path.exists() else None
    report['secondary'] = {}
    for arm in ARMS:
        generated = [row['arms'][arm] for row in rows if arm in row.get('arms', {})]
        calls = [call for entry in generated for call in entry.get('generation', {}).get('calls', [])]
        lengths = [len(call['prompt']) + len(call.get('system_prompt', '')) for call in calls]
        seconds = [call['provider']['seconds'] for call in calls
                   if call.get('provider') and isinstance(call['provider'].get('seconds'), (float, int))]
        audits = [entry['generation']['revision_audit'] for entry in generated
                  if 'revision_audit' in entry.get('generation', {})]
        refs = [ref for audit in audits for ref in audit['strategy_references']]
        report['secondary'][arm] = dict(generation_attempts=len(calls),
            generation_ready=sum(entry['generation_status'] == 'ready' for entry in generated),
            prompt_chars_median=statistics.median(lengths) if lengths else None,
            generation_seconds_median=statistics.median(seconds) if seconds else None,
            unchanged_payloads=sum(entry.get('unchanged_payload') is True for entry in generated),
            audited_generations=len(audits), references=len(refs),
            verified_references=sum(ref['source_verified'] for ref in refs),
            generations_with_verified_reference=sum(a['has_verified_reference'] for a in audits),
            interpretations_verified=False)
    for name, comparison in report['paired_comparisons'].items():
        left, right = name.split('_minus_')
        a, b = report['arms'][left]['case_means'], report['arms'][right]['case_means']
        differences = [a[cid] - b[cid] for cid in manifest['case_ids']
                       if a[cid] is not None and b[cid] is not None]
        comparison['descriptive_case_bootstrap_95_interval'] = bootstrap_case_interval(differences)
    report['case_trials'] = {}
    indexed = {row['case_id']: row for row in rows}
    for cid in manifest['case_ids']:
        report['case_trials'][cid] = {}
        for arm in ARMS:
            trials = indexed.get(cid, {}).get('arms', {}).get(arm, {}).get('trials', [])
            golds = {trial['repeat']: trial.get('gold') for trial in trials}
            report['case_trials'][cid][arm] = [
                'T' if golds.get(rep) is True else 'F' if golds.get(rep) is False else '?'
                for rep in range(REPETITIONS)]
    return report


def markdown(report):
    lines = ['# Revise 与 A2：固定历史开发实验结果', '',
        f"运行状态：`{report['run_status']}`。日期：2026-10-30（Asia/Shanghai）。四臂从同一已存档的有效失败 P0/H1 出发，"
        '每任务每臂生成一个 P1，再在独立重置的环境执行两次。官方 Gold 是成功依据。', '',
        '| 版本 | 确认成功 / 计划执行槽 | 有效结果 | 未决槽 | 请求字符数中位数 |',
        '|---|---:|---:|---:|---:|']
    for arm in ARMS:
        item, extra = report['arms'][arm], report['secondary'][arm]
        lines.append(f"| {LABELS[arm]} | {item['confirmed_successes']}/{item['planned_target_slots']} | "
                     f"{item['valid_target_results']} | {item['missing_slots']} | {extra['prompt_chars_median']} |")
    lines += ['', '原始逐任务结果：T=官方成功，F=官方失败，?=错误或未执行。两次重复共享同一个候选，不是两个独立任务。', '',
              '| 任务 | 完整策略库 | 旧 A2 | 新检索 | 新检索＋反馈校准 |', '|---|---|---|---|---|']
    for cid, arms in report['case_trials'].items():
        lines.append('| ' + cid + ' | ' + ' | '.join('/'.join(arms[arm]) or '?' for arm in ARMS) + ' |')
    lines += ['', '| 配对比较 | 完整任务对 | 胜 / 负 / 平 | 每任务平均差 | 描述性任务 bootstrap 95% 区间 |',
              '|---|---:|---:|---:|---:|']
    for name, comp in report['paired_comparisons'].items():
        left, right = name.split('_minus_')
        delta = comp['complete_case_mean_difference']
        interval = comp['descriptive_case_bootstrap_95_interval']
        lines.append(f"| {LABELS[left]} − {LABELS[right]} | {comp['complete_case_pairs']} | "
            f"{comp['case_wins']}/{comp['case_losses']}/{comp['case_ties']} | "
            + (f'{delta:+.1%}' if delta is not None else '未知') + ' | '
            + (f'[{interval[0]:+.1%}, {interval[1]:+.1%}]' if interval else '未知') + ' |')
    if report.get('scheduling_amendment'):
        lines += ['', '并发修订：按用户运行中的提速要求，尚未启动的 c005/c006 提前交接给并行协调器，'
            '每任务四臂同时生成；全部候选返回后，每轮各臂并行评分，两轮之间有屏障。'
            '原三个任务继续原调度。候选数、评分数、源历史、模型参数均未改变，未重放已开始的调用。'
            '该调整改变了并发负载和时序，应作为实验条件记录；旧调度器的预期文件认领冲突另行归档，真实模型错误保留。']
    audit = report['secondary']['a2_v2_calibrated']
    lines += ['', f"反馈校准臂：{audit['generations_with_verified_reference']}/{audit['audited_generations']} "
        f"个修订包含至少一条有效策略引用；{audit['verified_references']}/{audit['references']} 条引文能在对应策略中逐字核对。"
        '分母只含完成引用审计的生成；生成错误另见主表。'
        '这只验证来源，不能证明模型理解或因果归因正确。', '',
        '解释限制：这是五个已用开发任务的条件比较；每臂每任务只有一次候选生成。'
        '错误保留计划分母，未决不是确定失败。描述性区间以任务为重采样单位，样本过小，不能据此宣称显著或泛化提升。'
        '若已观察任务的差值全部相同，经验 bootstrap 区间会退化；这不表示总体或生成随机性的不确定性为零。'
        '在线 v2 同时改变选例规则、字段编码与显式上下文引用展开，无法单独归因其中一项。', '',
        '后续验证应预先选定新的任务组、增加独立候选生成重复，并将“仅改选择器”单独作为一臂；'
        '当前保留该离线消融资源，未把代理检索指标当成成功率。', '',
        f"运行数据：[{Path(report['run']).name}]({report['run']}/summary.json)。"
        '实验协议见 [预定协议](revise_ablation_protocol_20261030.md)。']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=ROOT / 'runs/revise_ablation_20261030')
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/revise_ablation_results_20261030')
    args = parser.parse_args()
    report = analyze(args.run)
    write_json(args.output.with_suffix('.json'), report)
    args.output.with_suffix('.md').write_text(markdown(report))
    print(json.dumps({'status': report['run_status'], 'report': str(args.output.with_suffix('.md'))}))


if __name__ == '__main__':
    main()
