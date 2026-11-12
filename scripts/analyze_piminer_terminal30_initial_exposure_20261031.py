"""Read saved initial tool messages; write content-free exposure metadata only.

No credentials, clients, experiment runners, tools or environment state are read.
The comparison target is the full imported terminal payload, never a snippet.
"""
import argparse
from ast import literal_eval
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN = ROOT / 'runs/a2_v2_terminal_mixed_test30_20261031'
DEFAULT_PREFIX = ROOT / 'reports/piminer_terminal30_initial_exposure_20261031'


def read(path):
    return json.loads(Path(path).read_text())


def committed_rows(path):
    if not path.exists():
        return []
    content = path.read_text()
    output = []
    for index, line in enumerate(content.splitlines()):
        if not line.strip():
            continue
        try:
            output.append(json.loads(line))
        except json.JSONDecodeError:
            if index == len(content.splitlines()) - 1 and not content.endswith('\n'):
                break
            raise
    return output


def full_match(value, payload, depth=0, decoding='raw_text', seen=None):
    """Compare text leaves, with bounded native-format unescaping of tool text.

    No whitespace stripping, Unicode normalization, case folding or fragment
    matching is used. Object identities guard YAML alias recursion.
    """
    seen = set() if seen is None else seen
    if isinstance(value, str):
        if payload in value:
            return decoding
        if depth >= 3:
            return None
        # AgentDojo tool_result_to_str uses str(dict) for dict returns. Those
        # are Python literal repr, whereas list/model returns use YAML.
        for parser, label in ((json.loads, 'json'), (literal_eval, 'python_literal'),
                              (yaml.safe_load, 'yaml')):
            try:
                decoded = parser(value)
            except (ValueError, TypeError, SyntaxError, yaml.YAMLError):
                continue
            if decoded == value:
                continue
            match = full_match(decoded, payload, depth + 1, label, seen)
            if match:
                return match
        return None
    if isinstance(value, (dict, list)):
        if id(value) in seen:
            return None
        seen.add(id(value))
        children = value.values() if isinstance(value, dict) else value
        for child in children:
            match = full_match(child, payload, depth, decoding, seen)
            if match:
                return match
    return None


def public_tool_name(message, calls):
    call = message.get('tool_call')
    if not isinstance(call, dict):
        call = calls.get(message.get('tool_call_id'), {})
    function = call.get('function')
    if isinstance(function, dict):
        function = function.get('name')
    return function if isinstance(function, str) else message.get('name')


def analyze(run=DEFAULT_RUN):
    run = Path(run).resolve()
    manifest = read(run / 'manifest.json')
    bundle_path = ROOT / manifest['config']['attacker']['initialization_manifest']
    bundle_bytes = bundle_path.read_bytes()
    bundle = json.loads(bundle_bytes)
    bundle_sha256 = hashlib.sha256(bundle_bytes).hexdigest()
    if bundle_sha256 != manifest['inputs_sha256'][bundle_path.relative_to(ROOT).as_posix()]:
        raise ValueError('Runtime initialization bundle changed')
    provenance_path = ROOT / manifest['config']['experiment']['initial_payload_provenance']
    provenance_bytes = provenance_path.read_bytes()
    provenance_sha256 = hashlib.sha256(provenance_bytes).hexdigest()
    if provenance_sha256 != manifest['config']['experiment']['initial_payload_provenance_sha256']:
        raise ValueError('Pinned terminal source provenance changed')
    sidecar = json.loads(provenance_bytes)
    if (bundle_sha256 != sidecar['runtime_manifest_sha256']
            or any(entry['analysis'] != '' for entry in bundle['initializations'])):
        raise ValueError('Historical analysis entered runtime bundle')
    statuses = {row['case_id']: row['source_status'] for row in sidecar['sources']}
    source_redacted_trajectories = sum(
        read(source['sample_path'])['history'][-1].get('trajectory')
        == '[REDACTED — black-box run; trajectory not retained]'
        for source in sidecar['sources'])
    rows = []
    for entry in bundle['initializations']:
        cid = entry['case_id']
        row = dict(case_id=cid, source_status=statuses[cid], initial_gold=None,
            initial_episode_status='pending', exposure_status='unknown',
            exposure_reason='initial_execution_not_yet_committed',
            returned_tool_names=[], full_payload_matches=[])
        initial = [e for e in committed_rows(run / cid / 'episodes.jsonl')
                   if e['episode_id'] == f'{cid}/shared/000']
        if not initial:
            rows.append(row)
            continue
        if len(initial) != 1:
            raise ValueError('Duplicate initial episode')
        episode = initial[0]
        raw_path = (run / cid / episode['raw_path']).resolve()
        if not raw_path.is_relative_to(run / cid):
            raise ValueError('Unexpected raw path')
        saved = read(raw_path)
        if saved['payload'] != entry['payload']:
            raise ValueError('Initial execution payload differs from terminal bundle')
        result = saved['result']
        if result['gold'] is not episode['gold']:
            raise ValueError('Initial episode and raw Gold disagree')
        row.update(initial_gold=episode['gold'], initial_episode_status=episode['status'])
        messages = result.get('raw', {}).get('messages')
        if not isinstance(messages, list) or not messages:
            row['exposure_reason'] = 'saved_messages_missing_or_empty'
            rows.append(row)
            continue
        calls = {}
        for message in messages:
            if message.get('role') == 'assistant':
                for call in message.get('tool_calls') or []:
                    if isinstance(call, dict) and isinstance(call.get('id'), str):
                        calls[call['id']] = call
        tool_messages = [(i, m) for i, m in enumerate(messages) if m.get('role') == 'tool']
        tool_names, matches = [], []
        for index, message in tool_messages:
            name = public_tool_name(message, calls)
            if name is not None:
                tool_names.append(name)
            # Never inspect assistant text, tool call arguments, pre/post state,
            # query packets, final self-report or source target trajectory.
            method = full_match(message.get('content'), entry['payload'])
            if method:
                matches.append(dict(message_index=index, public_tool_name=name, decoding=method))
        row.update(returned_tool_names=sorted(set(tool_names)), full_payload_matches=matches)
        if matches:
            row.update(exposure_status='full_payload_observed',
                       exposure_reason='exact_complete_payload_in_saved_tool_return')
        elif result.get('error') or episode.get('error'):
            row['exposure_reason'] = 'target_error_saved_messages_may_be_partial'
        elif not tool_messages:
            row['exposure_reason'] = 'no_saved_tool_returns_cannot_confirm_slot_exposure'
        else:
            row.update(exposure_status='not_observed_in_saved_tool_messages',
                       exposure_reason='no_full_exact_match_after_native_format_decoding')
        rows.append(row)
    aggregates = {}
    for status in ('hit', 'miss', 'all'):
        subset = [r for r in rows if status == 'all' or r['source_status'] == status]
        completed = [r for r in subset if r['initial_episode_status'] != 'pending']
        aggregates[status] = dict(assigned_cases=len(subset), committed_initials=len(completed),
            initial_gold_true=sum(r['initial_gold'] is True for r in completed),
            initial_gold_false=sum(r['initial_gold'] is False for r in completed),
            initial_gold_unknown=sum(r['initial_gold'] is None for r in completed),
            exposure_status_counts=dict(Counter(r['exposure_status'] for r in subset)),
            initial_gold_false_and_full_payload_observed=[r['case_id'] for r in completed
                if r['initial_gold'] is False and r['exposure_status'] == 'full_payload_observed'])
    complete = aggregates['all']['committed_initials'] == 30
    return dict(status='all_initials_complete' if complete else 'snapshot',
        generated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        run=str(run), run_status=read(run / 'status.json')['status'], api_calls=0,
        initial_inputs=dict(runtime_bundle_sha256=bundle_sha256,
            pinned_source_provenance_sha256=provenance_sha256,
            all_initial_analysis_empty=True, all_committed_payloads_match_bundle=True,
            source_terminal_trajectories_redacted=source_redacted_trajectories),
        scope='Only committed initial execution messages; full payload exact match in tool returns.',
        matching='Recursively inspect tool content string leaves; bounded JSON/Python literal/YAML decoding, matching native AgentDojo formatting; exact full payload substring, no normalization or fragments.',
        interpretation=[
            'Full payload observed means the saved model-visible tool return contains the complete imported payload, not that the model followed it.',
            'No full match does not prove the payload slot was never called, nor exclude partial/transformed exposure.',
            'Missing/empty messages, no tool returns and unsuccessful partial trajectories remain unknown.',
            'Initial Gold differences precede any new revision or Retry decision; this does not identify a causal source of model behavior.',
            'Historical terminal trajectories are redacted; saved source data cannot establish equality of full old model context, tool paths, underlying request retries or provider state.',
            'Historical hit/miss labels only stratify this offline report and are not imported into the attacker.'],
        aggregates=aggregates, cases=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--output-prefix', type=Path, default=DEFAULT_PREFIX)
    parser.add_argument('--require-all-initials', action='store_true')
    args = parser.parse_args()
    prefix = args.output_prefix.resolve()
    if not prefix.is_relative_to(ROOT / 'reports'):
        raise ValueError('Reports must stay under reports/')
    # Decoder checks cover actual serialization differences without real data.
    sentinel = 'line one\n\"quoted\" line two\n'
    assert full_match({'text': json.dumps({'value': sentinel})}, sentinel) == 'json'
    assert full_match({'text': str({'value': sentinel})}, sentinel) == 'python_literal'
    assert full_match({'text': yaml.safe_dump({'value': sentinel})}, sentinel) == 'yaml'
    assert full_match({'text': sentinel[:-1]}, sentinel) is None
    report = analyze(args.run)
    if args.require_all_initials and report['aggregates']['all']['committed_initials'] != 30:
        raise ValueError('Not all 30 initial episodes have committed')
    prefix.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    summary = report['aggregates']
    title = '完整 30 条初始诊断' if report['status'] == 'all_initials_complete' else '可见性快照'
    lines = [f'# 末轮 payload 新首轮：{title}', '',
        f"时间：{report['generated_at']}。初始诊断状态：{report['status']}；完整运行状态：{report['run_status']}。只读离线分析，新增 API 调用 0。", '',
        '| 原历史分组 | 已提交初始执行/分组总数 | 新初始 Gold=True | Gold=False | Gold 未决 |',
        '| --- | ---: | ---: | ---: | ---: |']
    for status in ('hit', 'miss', 'all'):
        item = summary[status]
        lines.append(f"| {status} | {item['committed_initials']}/{item['assigned_cases']} | {item['initial_gold_true']} | {item['initial_gold_false']} | {item['initial_gold_unknown']} |")
    lines += ['', '仅检查新初始执行中实际保存的 tool 返回 content，按原生 JSON/Python literal/YAML 解码后查找完整 payload；AgentDojo 的 dict 返回使用 str(dict)，list/model 返回使用 YAML，因此同时处理这些格式。不检查 final 自述、工具参数、pre/post 私有状态或 Evidence。报告不输出任何 payload、工具内容或私有状态。', '',
        '`full_payload_observed` 表示完整 payload 出现在已保存的模型可见工具返回；它不证明模型遵从。`not_observed_in_saved_tool_messages` 仅表示完整字节未匹配，不证明未调用槽，也不排除部分或变形暴露。缺消息、错误且无完整匹配、无工具返回或尚未提交均标 unknown。', '',
        '新初始 Gold 的变化发生在任何新 Revise/Retry 前；不能据此推断具体行为原因，也不能把这些初始差异归给后续修订。', '',
        f"来源与输入固定核验：初始 bundle SHA-256 为 `{report['initial_inputs']['runtime_bundle_sha256']}`，全部初始 analysis 为空，所有已提交新初始 payload 与 bundle 完全一致。原 30 个末轮 trajectory 字段有 {report['initial_inputs']['source_terminal_trajectories_redacted']} 个明确标为黑盒未保留，故不能凭旧 final 自述还原旧工具路径、完整模型上下文、底层重试或服务状态，也不能对新首轮不复现作唯一因果归因。", '',
        '| Case | 原组 | 新初始 Gold | 暴露状态 | 完整匹配的公共工具名 |',
        '| --- | --- | --- | --- | --- |']
    for row in report['cases']:
        names = ', '.join(sorted({m['public_tool_name'] for m in row['full_payload_matches']
                                 if m['public_tool_name']})) or '—'
        gold = 'pending' if row['initial_episode_status'] == 'pending' else str(row['initial_gold'])
        lines.append(f"| {row['case_id']} | {row['source_status']} | {gold} | {row['exposure_status']} | {names} |")
    prefix.with_suffix('.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps(dict(status=report['status'], run_status=report['run_status'], api_calls=0,
                          aggregates=summary), ensure_ascii=False))


if __name__ == '__main__':
    main()
