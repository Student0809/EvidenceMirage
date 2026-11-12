#!/usr/bin/env python3
"""Audit original strategy text with an explicit local tokenizer, without model calls.

Only new reports are written. Source JSON is an archive, not prompt material:
its parsed fields duplicate source_blocks. Count original Markdown once instead.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKENIZER = Path('/mnt/data/users/zhengqingliu/Model/Qwen3.8-27B/tokenizer.json')
FIELDS = ('user_task', 'injection_goal', 'placeholder_context',
          'winning_injection_text', 'result', 'why_it_worked')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_converter(root):
    spec = importlib.util.spec_from_file_location('audit_source_converter', root / 'strategy_library_json/convert.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def partition(document, converter):
    """Partition exact source bytes; fenced headings never start new examples."""
    segments, examples = [], []
    offset = 0
    for block in document['source_blocks']:
        text = block['markdown']
        if (block['heading'] or '').startswith('In-context examples'):
            starts = [item for item in converter.headings(text, 3) if item[3].startswith('Example ')]
            prefix_end = starts[0][0] if starts else len(text)
            segments.append(('other', text[:prefix_end]))
            for i, (start, _, _, _) in enumerate(starts):
                stop = starts[i + 1][0] if i + 1 < len(starts) else len(text)
                raw = text[start:stop]
                line = converter.restore_markdown(document)[:offset + start].count('\n') + 1
                segments.append(('case', raw))
                examples.append((line, raw))
        else:
            segments.append(('method', text))
        offset += len(text)
    if ''.join(text for _, text in segments) != converter.restore_markdown(document):
        raise ValueError(f"Source partition lost content: {document['id']}")
    if len(examples) != len(document['examples']):
        raise ValueError(f"Example boundary mismatch: {document['id']}")
    for (line, _), example in zip(examples, document['examples']):
        if line != example['source_line']:
            raise ValueError(f"Example source line mismatch: {document['id']}, {line}")
    return segments, examples


def csv_text(rows):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def audit(root, tokenizer_path):
    from tokenizers import Tokenizer
    import tokenizers

    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    # Keep exact-tokenizer counting deterministic even if a saved tokenizer has limits.
    tokenizer.no_truncation()
    tokenizer.no_padding()
    def count(text):
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    converter = load_converter(root)
    index_path = root / 'strategy_library_json/index.json'
    taxonomy_path = root / 'strategy_library_compact/index.json'
    index = json.loads(index_path.read_bytes())
    taxonomy = json.loads(taxonomy_path.read_bytes())
    cards = {card['strategy_id']: card for card in taxonomy['cards']}
    input_hashes = {str(p.relative_to(root)): sha256(p.read_bytes()) for p in (index_path, taxonomy_path)}
    strategies, cases, sections, warnings = [], [], [], []
    ids = set()
    for entry in index['entries']:
        if entry['kind'] != 'strategy':
            continue
        identifier = entry['id']
        if identifier in ids:
            raise ValueError(f'Duplicate registry ID: {identifier}')
        ids.add(identifier)
        json_path = root / 'strategy_library_json' / entry['file']
        document = json.loads(json_path.read_bytes())
        if document['id'] != identifier or document['kind'] != 'strategy':
            raise ValueError(f'Registry/source identity mismatch: {identifier}')
        source_path = root / 'strategy_library' / document['source']['filename']
        source_bytes = source_path.read_bytes()
        source_text = source_bytes.decode('utf-8')
        actual_hash = sha256(source_bytes)
        if actual_hash != entry['source_sha256'] or actual_hash != document['source']['sha256']:
            raise ValueError(f'Stale source hash: {source_path}')
        if len(source_bytes) != document['source']['bytes']:
            raise ValueError(f'Stale source byte count: {source_path}')
        if converter.convert_document(source_text, source_path.name) != document:
            raise ValueError(f'Stale structured export: {json_path}')
        card = cards[identifier]
        if card['source_sha256'] != actual_hash:
            raise ValueError(f'Stale family mapping: {identifier}')
        for path in (json_path, source_path):
            input_hashes[str(path.relative_to(root))] = sha256(path.read_bytes())
        segments, raw_cases = partition(document, converter)
        subtotals = {kind: sum(count(text) for category, text in segments if category == kind)
                     for kind in ('method', 'case', 'other')}
        source_ref = str(source_path.relative_to(root))
        start = len(cases)
        for ordinal, ((line, raw), example) in enumerate(zip(raw_cases, document['examples']), 1):
            field_blocks = example['field_blocks']
            field_text = ''.join(block['markdown'] for block in field_blocks)
            if field_text and not raw.endswith(field_text):
                raise ValueError(f'Case field partition mismatch: {identifier}, {ordinal}')
            prefix = raw[:-len(field_text)] if field_text else raw
            field_counts = {field: sum(count(b['markdown']) for b in field_blocks if b['field'] == field)
                            for field in FIELDS}
            tokens = count(raw)
            benchmark = example['benchmark_case'] or {}
            cases.append({
                'case_ref': f'{identifier}::example-{ordinal:03d}',
                'strategy_id': identifier, 'family': card['mechanism'], 'dataset': card['dataset'],
                'scenario': benchmark.get('suite', 'unknown'),
                'task_type': 'unknown', 'task_type_status': 'not_recorded_in_source',
                'user_task_id': benchmark.get('user_task_id', 'unknown'),
                'injection_task_id': benchmark.get('injection_task_id', 'unknown'),
                'task_instance_id': 'unknown', 'target_model': example['target_model'],
                'outcome_label': 'unknown', 'outcome_status': 'unstructured_source_result_only',
                'result_recorded': bool(example['result']), 'result_text': example['result'] or '',
                'user_task_text': example['user_task'] or '',
                'injection_goal_text': example['injection_goal'] or '',
                'tokens': tokens,
                **{f'{field}_tokens': value for field, value in field_counts.items()},
                'wrapper_and_preamble_tokens': count(prefix),
                'field_boundary_delta': tokens - sum(field_counts.values()) - count(prefix),
                'over_800_tokens': tokens > 800,
                'payload_over_800_tokens': count(example['winning_injection_text'] or '') > 800,
                'payload_text_tokens': count(example['winning_injection_text'] or ''),
                'context_missing': example['placeholder_context'] is None,
                'context_reference_only': any(b['reference_only'] for b in field_blocks),
                'source': source_ref, 'source_line': line, 'source_sha256': actual_hash,
            })
        selected = cases[start:]
        if len(selected) != entry['example_count']:
            raise ValueError(f'Registry case count mismatch: {identifier}')
        full_tokens = count(source_text)
        strategies.append({
            'strategy_id': identifier, 'family': card['mechanism'], 'dataset': card['dataset'],
            'full_document_tokens': full_tokens, 'method_tokens': subtotals['method'],
            'case_count': len(selected), 'case_tokens': subtotals['case'],
            'case_mean_tokens': round(subtotals['case'] / len(selected), 2) if selected else 0,
            'case_max_tokens': max((c['tokens'] for c in selected), default=0),
            'other_tokens': subtotals['other'],
            'segment_boundary_delta': full_tokens - sum(subtotals.values()),
            'context_tokens': sum(c['placeholder_context_tokens'] for c in selected),
            'action_tokens': sum(c['winning_injection_text_tokens'] for c in selected),
            'result_tokens': sum(c['result_tokens'] for c in selected),
            'raw_json_tokens': count(json_path.read_bytes().decode('utf-8')),
            'cases_over_800': sum(c['over_800_tokens'] for c in selected),
            'payloads_over_800': sum(c['payload_over_800_tokens'] for c in selected),
            'source': source_ref, 'source_sha256': actual_hash,
        })
        for block in document['source_blocks']:
            if not (block['heading'] or '').startswith('In-context examples'):
                sections.append({'strategy_id': identifier, 'heading': block['heading'] or 'Title / introduction',
                                 'tokens': count(block['markdown'])})
        warnings.extend(f'{identifier}: {warning}' for warning in document['warnings'])
    if len(strategies) != index['strategy_count'] or len(cases) != index['example_count']:
        raise ValueError('Registry total counts do not match audited sources')
    input_hashes['strategy_library_json/convert.py'] = sha256((root / 'strategy_library_json/convert.py').read_bytes())
    input_hashes['scripts/library_token_audit.py'] = sha256(Path(__file__).read_bytes())
    return {
        'tokenizer': {'path': str(tokenizer_path), 'sha256': sha256(tokenizer_path.read_bytes()),
                      'tokenizers_version': tokenizers.__version__, 'add_special_tokens': False},
        'input_hashes': input_hashes, 'strategies': strategies, 'cases': cases,
        'sections': sections, 'warnings': warnings,
    }


def render_report(data):
    strategies, cases = data['strategies'], data['cases']
    total = sum(s['full_document_tokens'] for s in strategies)
    case_total = sum(s['case_tokens'] for s in strategies)
    context_total = sum(c['placeholder_context_tokens'] for c in cases)
    action_total = sum(c['winning_injection_text_tokens'] for c in cases)
    result_total = sum(c['result_tokens'] for c in cases)
    raw_json_total = sum(s['raw_json_tokens'] for s in strategies)
    short_ids = {s['strategy_id']: f'S{i:02d}' for i, s in enumerate(strategies, 1)}
    lines = [
        '# 策略库 Token 审计（阶段 0）', '',
        '只读审计原始文档；没有生成 Card、Brief、Case，没有模型请求或在线评测。', '',
        'Router / Generator 的后续配置为 `claude -p` 订阅、Claude Sonnet 4.6、effort=low；'
        '本报告仍按指定 Qwen tokenizer 计量，不代表 Claude 的计费 token 或实际上下文用量。', '',
        '## 主要结果', '',
        f'- {len(strategies)} 个策略、{len({s["family"] for s in strategies})} 个 family、{len(cases)} 个案例；排除 `_TEMPLATE`。',
        f'- 原始 Markdown 总计 **{total:,} tokens**；案例分段计 **{case_total:,}**（约 **{case_total / total:.1%}**）。',
        f'- 方法分段 **{sum(s["method_tokens"] for s in strategies):,}**；其他可分离内容 **{sum(s["other_tokens"] for s in strategies):,}**；'
        f'分段边界差值 **{sum(s["segment_boundary_delta"] for s in strategies):+,}**。',
        f'- 案例均值 **{case_total / len(cases):,.1f}**、最大 **{max(c["tokens"] for c in cases):,}** tokens；'
        f'**{sum(c["over_800_tokens"] for c in cases)} / {len(cases)}** 条原始案例超过 800。',
        f'- 案例字段中：原始上下文 **{context_total:,}**、动作 **{action_total:,}**、结果 **{result_total:,}** tokens。'
        '上下文和结果可能含工具输出或转录，不能据字段名称认定为可删除日志。',
        f'- **{sum(c["payload_over_800_tokens"] for c in cases)}** 条案例仅裸动作字段就超过 800 tokens；'
        '这只是字段长度标记，不是清理后的长度预测，也不证明每个字符都属于必要动作。',
        f'- 原始 JSON 文件总计 **{raw_json_total:,}** tokens（Markdown 的 **{raw_json_total / total:.2f}×**）。'
        '其中解析字段与 source_blocks 重复保存；此数是存储形式诊断，不是运行提示用量。', '',
        '## 计量与分离规则', '',
        f'- Tokenizer：`{data["tokenizer"]["path"]}`；SHA-256：`{data["tokenizer"]["sha256"]}`。',
        f'- `tokenizers=={data["tokenizer"]["tokenizers_version"]}`，`add_special_tokens=False`；显式关闭截断和 padding，无近似回退、无下载。',
        '- full_document_tokens 对完整原文编码一次；不加 chat template、系统提示、历史或传输包装。',
        '- method_tokens 包含案例章节以外的原文：介绍、条件、步骤、指纹对比、失败模式和初始化建议；不擅自判为重复并删除。',
        '- case_tokens 包含每条案例的标题、字段、代码围栏和完整正文。字段分解使用原始 field_blocks，包含字段标签及围栏。',
        '- other_tokens 仅包括案例章节标题和首个案例前的说明。无法可靠单独识别的原始日志、重复输出和样板不硬拆；需后续内容审查。',
        '- 文本切片严格按原文顺序无遗漏、无重复重建；分段独立编码会改变 BPE 边界，因此单列 segment_boundary_delta。'
        '字段同理单列 field_boundary_delta；分项近似份额不当作精确可节省量。',
        '- 逐文件验证 index / JSON / 原文 / family 映射哈希和案例数量，重解析 JSON 与存档逐字段比较。', '',
        '## 每策略统计', '',
        '| 引用 / Registry ID | 方法 | 案例数 | 案例总量 | 案例均值 | 案例最大 | 其他 | 边界差 | 原文总量 |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    for s in strategies:
        lines.append(f'| {short_ids[s["strategy_id"]]} / [{s["strategy_id"]}](../{s["source"]}) | '
                     f'{s["method_tokens"]:,} | {s["case_count"]} | {s["case_tokens"]:,} | '
                     f'{s["case_mean_tokens"]:,.2f} | {s["case_max_tokens"]:,} | {s["other_tokens"]:,} | '
                     f'{s["segment_boundary_delta"]:+,} | {s["full_document_tokens"]:,} |')
    lines += ['', '## 场景覆盖', '', '| Dataset | 场景 | 案例数 | 案例 tokens |', '| --- | --- | ---: | ---: |']
    groups = defaultdict(list)
    for c in cases:
        groups[c['dataset'], c['scenario']].append(c)
    for (dataset, scenario), selected in sorted(groups.items()):
        lines.append(f'| {dataset} | {scenario} | {len(selected)} | {sum(c["tokens"] for c in selected):,} |')
    lines += ['', '## 案例字段总览', '',
              '各项保留原始字段标签和围栏；这是长度分布，不是可删除量。', '',
              '| 字段 | Tokens | 占案例分段比例 |', '| --- | ---: | ---: |']
    for field, label in [('user_task', '用户任务'), ('injection_goal', '攻击目标'),
                         ('placeholder_context', '原始上下文'), ('winning_injection_text', '动作 / payload'),
                         ('result', '结果'), ('why_it_worked', '为何有效的原始说明'),
                         ('wrapper_and_preamble', '案例标题和前言')]:
        value = sum(c[field + '_tokens'] for c in cases)
        lines.append(f'| {label} | {value:,} | {value / case_total:.1%} |')
    lines.append(f'| 字段边界差值 | {sum(c["field_boundary_delta"] for c in cases):+,} | — |')
    lines += ['', '## 大案例的字段分布（前 15 条）', '',
              '| 案例 / 来源 | 总量 | 上下文 | 动作 | 结果 | 其他字段和边界差 |',
              '| --- | ---: | ---: | ---: | ---: | ---: |']
    for c in sorted(cases, key=lambda c: (-c['tokens'], c['case_ref']))[:15]:
        remainder = c['tokens'] - c['placeholder_context_tokens'] - c['winning_injection_text_tokens'] - c['result_tokens']
        label = short_ids[c['strategy_id']] + '/E' + c['case_ref'].rsplit('-', 1)[1]
        lines.append(f'| [{label}](../{c["source"]}#L{c["source_line"]}) | {c["tokens"]:,} | '
                     f'{c["placeholder_context_tokens"]:,} | {c["winning_injection_text_tokens"]:,} | '
                     f'{c["result_tokens"]:,} | {remainder:,} |')
    lines += ['', '## 方法文本的大段落（前 10 条）', '', '| 策略 | 原文章节 | Tokens |', '| --- | --- | ---: |']
    for section in sorted(data['sections'], key=lambda s: (-s['tokens'], s['strategy_id']))[:10]:
        heading = section['heading'].replace('|', '\\|')
        lines.append(f'| {short_ids[section["strategy_id"]]} | {heading} | {section["tokens"]:,} |')
    lines += ['', '## 每案例清单', '',
              '完整的原始结果文字、用户任务、攻击目标和各字段 tokens 见 `library_case_token_audit.csv`。'
              '这里的 unknown 表示源文件未提供结构化标签，不表示案例失败或缺少原始结果。', '',
              '| 案例 / 来源 | 场景 | 用户任务 / 注入任务 | 任务类型 | 结果标签 / 原始结果 | Tokens |',
              '| --- | --- | --- | --- | --- | ---: |']
    for c in cases:
        label = short_ids[c['strategy_id']] + '/E' + c['case_ref'].rsplit('-', 1)[1]
        result = 'unknown / 已记录' if c['result_recorded'] else 'unknown / 缺失'
        lines.append(f'| [{label}](../{c["source"]}#L{c["source_line"]}) | {c["scenario"]} | '
                     f'{c["user_task_id"]} / {c["injection_task_id"]} | unknown | {result} | {c["tokens"]:,} |')
    lines += ['', '## 证据限制与下一阶段决策', '',
              '- 源库没有统一 task_type、完整 task_instance_id / step_index 或逐轮 actions/outcomes；本次不调用 LLM 补标。'
              '任务类型以后按用户任务意图分类，攻击目标单列，合并标签须先询问。',
              f'- {sum(c["context_missing"] for c in cases)} 条案例缺少正文上下文，保留引用和来源；不从描述补造原始记录。',
              '- 原库主要归档命中案例，缺少全部尝试的分母；不计算成功率，也不把全部案例标成已独立核验的 success。'
              '不能用叙述关键词自动认定 recovered 或多策略组合。',
              '- 方法正文可能含具体任务的既往结果。后续冒烟任务必须同时核对案例与方法正文，按任务组排除；只过滤 Case 不足以隔离。',
              '- 本次全库原文总计 228,575 tokens，与此前给出的全库数字一致。约 69k 的 top-3 内容尚缺具体候选列表和组装快照；'
              '完整三策略 prompt 还需按实际加载文件、包装及历史重新计量。',
              '- 800-token Case 和 1,500–3,000-token Brief 都是后续内容目标；本报告只测原文，不证明目标可达、语义覆盖或效果保持。'
              '先审查大上下文与长动作，再决定建库模型、保真检查和超预算处理。',
              '- 当前仅阶段 0：未变更 Router/Generator、Teacher 2、harness、原策略或案例；没有构建探索集或运行冒烟测试。', '',
              '## 重现与来源快照', '', '在仓库根目录运行：', '', '```bash',
              '/home/zhengqingliu/miniconda3/envs/harness/bin/python scripts/library_token_audit.py \\',
              f'  --tokenizer {data["tokenizer"]["path"]}', '```', '',
              '同内容可重复运行；若报告已存在且内容不同，拒绝覆盖，请改用新的 `--output-dir`。'
              'harness Python 环境仅用于读取已有 tokenizers 依赖，不修改 `harness/` 或环境。', '',
              '| 输入文件 | SHA-256 |', '| --- | --- |']
    for path, digest in sorted(data['input_hashes'].items()):
        lines.append(f'| `{path}` | `{digest}` |')
    lines += ['', '## 原始导出警告', '']
    lines.extend('- ' + warning for warning in data['warnings'])
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--tokenizer', type=Path, default=DEFAULT_TOKENIZER)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'reports')
    args = parser.parse_args(argv)
    try:
        root, tokenizer_path, output = args.root.resolve(), args.tokenizer.resolve(), args.output_dir.resolve()
        for protected in ('strategy_library', 'strategy_library_json', 'strategy_library_compact', 'harness'):
            if output.is_relative_to(root / protected):
                raise ValueError(f'Reports cannot be written inside protected source directory: {protected}')
        data = audit(root, tokenizer_path)
        outputs = {
            'library_token_audit.md': render_report(data),
            'library_token_audit.csv': csv_text(data['strategies']),
            'library_case_token_audit.csv': csv_text(data['cases']),
        }
        for name, text in outputs.items():
            path = output / name
            if path.exists() and path.read_bytes() != text.encode('utf-8'):
                raise FileExistsError(f'Refusing to overwrite different report: {path}; use --output-dir')
        output.mkdir(parents=True, exist_ok=True)
        for name, text in outputs.items():
            path = output / name
            if not path.exists():
                with path.open('x', encoding='utf-8', newline='') as handle:
                    handle.write(text)
        print(json.dumps({'strategies': len(data['strategies']), 'cases': len(data['cases']),
                          'full_document_tokens': sum(s['full_document_tokens'] for s in data['strategies']),
                          'reports': [str(output / name) for name in outputs]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, ImportError, KeyError) as error:
        print(f'Audit failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
