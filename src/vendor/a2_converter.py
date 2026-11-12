#!/usr/bin/env python3
"""Lossless, offline Markdown-to-JSON export; never executes example content."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    'User task': 'user_task',
    'Injection goal': 'injection_goal',
    'Placeholder context': 'placeholder_context',
    'Winning injection text': 'winning_injection_text',
    'Resulting target': 'result',
    'Why this worked': 'why_it_worked',
}


def field_key(line):
    m = re.match(r'^\*\*([^*]+)\*\*', line)
    return next((name for prefix, name in FIELDS.items() if m and m[1].startswith(prefix)), None)


def scan(text, warnings=None):
    """Yield line offsets and structural status, respecting fence type/length.

    A ```bash line inside an existing fence is NOT a closing fence. Only a
    bare delimiter of the same type and at least the opening length closes it.
    """
    offset, fences, previous_close, last_field = 0, [], None, None
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        visible = line.rstrip('\r\n')
        m = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', visible)
        key = field_key(visible)
        order = list(FIELDS.values())
        expected = order[order.index(last_field) + 1] if last_field in order[:-1] else None
        if (fences and key == expected and previous_close and
                previous_close[0] == fences[0][0] and len(previous_close) >= len(fences[0])):
            # A known source has an unclosed outer context fence. Recover only
            # at the NEXT protocol field after an inner bare closing delimiter.
            fences.clear()
            if warnings is not None:
                warnings.append(f'Line {number}: recovered unclosed outer fence at {key}; source unchanged')
        structural = not fences and m is None
        if m:
            token, tail = m.groups()
            previous_close = None
            if not fences or tail.strip():
                fences.append(token)
            elif token[0] == fences[-1][0] and len(token) >= len(fences[-1]):
                fences.pop()
                previous_close = token
        elif visible.strip():
            previous_close = None
        if structural and key:
            last_field = key
        yield offset, offset + len(line), number, visible, structural
        offset += len(line)


def headings(text, level, warnings=None):
    pattern = re.compile(r'^' + '#' * level + r' (.+)$')
    return [(start, end, number, m[1])
            for start, end, number, line, structural in scan(text, warnings)
            if structural and (m := pattern.match(line))]


def code_blocks(text):
    result, fences, content_start, opening_start = [], [], 0, 0
    offset = 0
    for line in text.splitlines(keepends=True):
        m = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip('\r\n'))
        if m:
            token, tail = m.groups()
            if not fences:
                fences.append(token)
                content_start, opening_start = offset + len(line), offset
            elif tail.strip():
                fences.append(token)
            elif token[0] == fences[-1][0] and len(token) >= len(fences[-1]):
                fences.pop()
                if not fences:
                    result.append({'text': text[content_start:offset], 'start': opening_start,
                                   'end': offset + len(line)})
        offset += len(line)
    if fences:
        result.append({'text': text[content_start:], 'start': opening_start, 'end': len(text)})
    return result


def field_value(markdown):
    value = markdown.strip()
    blocks = code_blocks(value)
    if len(blocks) == 1 and not value[:blocks[0]['start']].strip() and not value[blocks[0]['end']:].strip():
        return blocks[0]['text']
    if len(blocks) > 1:
        # In a named field the first/last delimiters are its archival wrapper;
        # bare same-length inner fences otherwise look like sibling code blocks.
        opener = re.match(r'^ {0,3}(`{3,}|~{3,})[^\r\n]*\r?\n', value)
        if opener:
            closing = list(re.finditer(r'^ {0,3}' + re.escape(opener[1][0]) +
                                      '{' + str(len(opener[1])) + r',}[ \t\r]*$', value, re.M))
            if closing and not value[closing[-1].end():].strip():
                return value[opener.end():closing[-1].start()]
    lines = value.splitlines()
    if lines and all(not line.strip() or line.startswith('>') for line in lines):
        return '\n'.join(re.sub(r'^> ?', '', line) for line in lines).strip()
    return value or None


def label_tail(tail):
    """Separate an optional parenthetical annotation from an inline value."""
    rest = tail.lstrip()
    annotation = ''
    if rest.startswith('('):
        depth = 0
        for i, char in enumerate(rest):
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    annotation, rest = rest[:i + 1], rest[i + 1:]
                    break
    return annotation, re.sub(r'^\s*[:.]?\s*', '', rest)


def example(heading, body, source_line, warnings):
    number = re.match(r'Example\s+(\d+)', heading)
    target = re.search(r'\(target:\s*([^)]*)\)', heading)
    case = re.search(r'`([^`]+)`', heading)
    parts = [x.strip() for x in case[1].split('/')] if case else []
    result = {'number': int(number[1]) if number else None, 'heading': heading,
              'source_line': source_line, 'target_model': target[1].strip() if target else None,
              'benchmark_case': dict(zip(('suite', 'user_task_id', 'injection_task_id'), parts))
              if len(parts) == 3 else None,
              'preamble_markdown': '', 'placeholder_context_reference': None, 'field_blocks': []}
    result.update({name: None for name in FIELDS.values()})
    found = []
    for start, end, line_number, line, structural in scan(body):
        if not structural:
            continue
        m = re.match(r'^\*\*([^*]+)\*\*(.*)$', line)
        if m:
            key = next((name for prefix, name in FIELDS.items() if m[1].startswith(prefix)), None)
            if key:
                found.append((start, end, m[1], m[2], key))
    result['preamble_markdown'] = body[:found[0][0]].strip() if found else body.strip()
    for i, (start, end, label, tail, key) in enumerate(found):
        stop = found[i + 1][0] if i + 1 < len(found) else len(body)
        annotation, inline = label_tail(tail)
        markdown = inline + ('\n' if inline else '') + body[end:stop]
        value = field_value(markdown)
        # Some source documents supply only a pointer in the label annotation.
        # Keep that pointer, but do not claim it is a full verbatim context.
        reference_only = key == 'placeholder_context' and value is None and bool(annotation)
        if reference_only:
            result['placeholder_context_reference'] = annotation
        if result[key] is not None:
            warnings.append(f'{heading}: duplicate field {key}; see field_blocks for all values')
        else:
            result[key] = value
        result['field_blocks'].append({'field': key, 'label': label,
                                      'annotation_markdown': annotation,
                                      'markdown': body[start:stop], 'text': value,
                                      'reference_only': reference_only})
    for key in ('target_model', *FIELDS.values()):
        if result[key] is None:
            warnings.append(f'{heading}: missing {key}; retained as null')
    return result


def list_items(body, numbered=False):
    pattern = r'^(\d+)\.\s+(.*)$' if numbered else r'^[-*]\s+(.*)$'
    matches = []
    for start, end, number, line, structural in scan(body):
        if structural and (m := re.match(pattern, line)):
            matches.append((start, end, m))
    items = []
    for i, (start, end, m) in enumerate(matches):
        stop = matches[i + 1][0] if i + 1 < len(matches) else len(body)
        # An unindented new paragraph is supplemental prose, not part of a move.
        continuation = re.split(r'\r?\n\r?\n(?=\S)', body[end:stop], maxsplit=1)[0]
        content = (m[2] if numbered else m[1]) + '\n' + continuation
        label = re.match(r'^\*\*([^*]+)\*\*\s*[:—–.-]?\s*(.*)', content, re.S)
        item = {'text': label[2].strip() if label else content.strip(),
                'markdown': body[start:end] + continuation}
        item['name' if numbered else 'label'] = label[1] if label else None
        if numbered:
            item['number'] = int(m[1])
        items.append(item)
    return items


def table_cells(line):
    # Pipes in inline code and escaped pipes are not column separators.
    cells, cell, ticks, escaped = [], '', 0, False
    for token in re.findall(r'`+|[^`]', line.strip().strip('|')):
        if token.startswith('`'):
            ticks = 0 if ticks == len(token) else (len(token) if ticks == 0 else ticks)
        if token == '|' and not ticks and not escaped:
            cells.append(cell.strip())
            cell = ''
        else:
            cell += token
        escaped = token == '\\' and not escaped
    cells.append(cell.strip())
    return cells


def section(heading, body, numbered=False):
    result = {'heading': heading, 'markdown': body, 'items': list_items(body, numbered)}
    if numbered:
        result['steps'] = result.pop('items')
    return result


def fingerprint(heading, body):
    result = {'heading': heading, 'markdown': body, 'tables': []}
    lines = [line for _, _, _, line, structural in scan(body) if structural]
    for i, line in enumerate(lines):
        if not line.startswith('|') or i + 1 >= len(lines):
            continue
        divider = table_cells(lines[i + 1])
        if not divider or not all(re.fullmatch(r':?-{3,}:?', cell) for cell in divider):
            continue
        rows = []
        for row in lines[i + 2:]:
            if not row.startswith('|'):
                break
            rows.append(table_cells(row))
        result['tables'].append({'columns': table_cells(line), 'rows': rows})
    return result


def restore_markdown(document):
    return ''.join(block['markdown'] for block in document['source_blocks'])


def convert_document(text, filename):
    warnings = []
    h2 = headings(text, 2, warnings)
    h1 = headings(text, 1)
    preamble = text[:h2[0][0]] if h2 else text
    intro = preamble[h1[0][1]:].strip() if h1 else preamble.strip()
    doc = {'schema_version': '1.0', 'kind': 'template' if filename == '_TEMPLATE.md' else 'strategy',
           'id': Path(filename).stem, 'title': h1[0][3] if h1 else None,
           'source': {'filename': filename, 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
                      'bytes': len(text.encode('utf-8'))},
           'introduction_markdown': intro,
           'scope': {'target_models': None, 'user_and_injected_tasks': None},
           'mechanism_distinction': None, 'procedure': None, 'examples': [],
           'examples_preamble_markdown': '', 'fingerprint': None, 'failure_conditions': None,
           'initialization_guidance': None, 'extra_sections': [],
           'source_blocks': [{'heading': None, 'markdown': preamble}], 'warnings': warnings}
    for i, (start, end, number, heading) in enumerate(h2):
        stop = h2[i + 1][0] if i + 1 < len(h2) else len(text)
        body = text[end:stop]
        doc['source_blocks'].append({'heading': heading, 'markdown': text[start:stop]})
        if heading.startswith('Recommended target-LLM scope'):
            container, key, value = doc['scope'], 'target_models', section(heading, body)
        elif heading.startswith('Recommended user-task / injected-task scope'):
            container, key, value = doc['scope'], 'user_and_injected_tasks', section(heading, body)
        elif heading.startswith('Why this mechanism'):
            container, key, value = doc, 'mechanism_distinction', section(heading, body)
        elif heading.startswith('The ') and 'template' in heading:
            container, key, value = doc, 'procedure', section(heading, body, numbered=True)
        elif heading.startswith('In-context examples'):
            examples = [h for h in headings(body, 3) if h[3].startswith('Example ')]
            doc['examples_preamble_markdown'] += body[:examples[0][0]] if examples else body
            for j, (ex_start, ex_end, ex_line, ex_heading) in enumerate(examples):
                ex_stop = examples[j + 1][0] if j + 1 < len(examples) else len(body)
                doc['examples'].append(example(ex_heading, body[ex_end:ex_stop], number + ex_line, warnings))
            continue
        elif heading.startswith('Strategy fingerprint'):
            container, key, value = doc, 'fingerprint', fingerprint(heading, body)
        elif heading.startswith('When this strategy is expected to fail'):
            container, key, value = doc, 'failure_conditions', section(heading, body)
        elif heading.startswith('Notes for PAIR initialization'):
            container, key, value = doc, 'initialization_guidance', section(heading, body)
        else:
            doc['extra_sections'].append(section(heading, body))
            continue
        if container[key] is not None:
            warnings.append(f'Duplicate section {heading}; retained in extra_sections')
            doc['extra_sections'].append(value)
        else:
            container[key] = value
    numbers = [ex['number'] for ex in doc['examples']]
    if len(numbers) != len(set(numbers)):
        warnings.append('Duplicate example numbers; all examples retained in source order')
    if restore_markdown(doc) != text:
        raise ValueError(f'{filename}: source reconstruction failed')
    return doc


def json_schema():
    nullable_string = {'type': ['string', 'null']}
    item = {'type': 'object', 'required': ['text', 'markdown'],
            'properties': {'text': {'type': 'string'}, 'markdown': {'type': 'string'},
                           'label': nullable_string, 'name': nullable_string,
                           'number': {'type': 'integer', 'minimum': 1}}, 'additionalProperties': False}
    sec = {'type': ['object', 'null'], 'required': ['heading', 'markdown'],
           'properties': {'heading': {'type': 'string'}, 'markdown': {'type': 'string'},
                          'items': {'type': 'array', 'items': {'$ref': '#/$defs/item'}},
                          'steps': {'type': 'array', 'items': {'$ref': '#/$defs/item'}},
                          'tables': {'type': 'array', 'items': {'type': 'object',
                                     'required': ['columns', 'rows'], 'additionalProperties': False,
                                     'properties': {'columns': {'type': 'array', 'items': {'type': 'string'}},
                                                    'rows': {'type': 'array', 'items': {'type': 'array', 'items': {'type': 'string'}}}}}}}}
    field = {'type': 'object', 'required': ['field', 'label', 'annotation_markdown', 'markdown', 'text', 'reference_only'],
             'additionalProperties': False,
             'properties': {'field': {'enum': list(FIELDS.values())}, 'label': {'type': 'string'},
                            'annotation_markdown': {'type': 'string'}, 'markdown': {'type': 'string'},
                            'text': nullable_string, 'reference_only': {'type': 'boolean'}}}
    ex_props = {'number': {'type': ['integer', 'null']}, 'heading': {'type': 'string'},
                'source_line': {'type': 'integer', 'minimum': 1}, 'target_model': nullable_string,
                'benchmark_case': {'type': ['object', 'null'],
                                   'required': ['suite', 'user_task_id', 'injection_task_id'],
                                   'additionalProperties': False,
                                   'properties': {name: {'type': 'string'} for name in ('suite', 'user_task_id', 'injection_task_id')}},
                'preamble_markdown': {'type': 'string'},
                'placeholder_context_reference': nullable_string,
                'field_blocks': {'type': 'array', 'items': {'$ref': '#/$defs/field'}}}
    ex_props.update({name: nullable_string for name in FIELDS.values()})
    props = {'schema_version': {'const': '1.0'}, 'kind': {'enum': ['strategy', 'template']},
             'id': {'type': 'string'}, 'title': nullable_string,
             'source': {'type': 'object', 'required': ['filename', 'sha256', 'bytes'], 'additionalProperties': False,
                        'properties': {'filename': {'type': 'string'}, 'sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                                       'bytes': {'type': 'integer', 'minimum': 0}}},
             'introduction_markdown': {'type': 'string'},
             'scope': {'type': 'object', 'required': ['target_models', 'user_and_injected_tasks'],
                       'additionalProperties': False,
                       'properties': {name: {'$ref': '#/$defs/section'} for name in ('target_models', 'user_and_injected_tasks')}},
             'examples': {'type': 'array', 'items': {'type': 'object', 'required': list(ex_props),
                                                   'properties': ex_props, 'additionalProperties': False}},
             'examples_preamble_markdown': {'type': 'string'},
             'extra_sections': {'type': 'array', 'items': {'$ref': '#/$defs/section'}},
             'source_blocks': {'type': 'array', 'minItems': 1,
                               'items': {'type': 'object', 'required': ['heading', 'markdown'],
                                         'additionalProperties': False,
                                         'properties': {'heading': nullable_string, 'markdown': {'type': 'string'}}}},
             'warnings': {'type': 'array', 'items': {'type': 'string'}}}
    props.update({name: {'$ref': '#/$defs/section'} for name in
                  ('mechanism_distinction', 'procedure', 'fingerprint', 'failure_conditions', 'initialization_guidance')})
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            'title': 'PIMiner lossless strategy export', 'type': 'object',
            'required': list(props), 'properties': props, 'additionalProperties': False,
            '$defs': {'item': item, 'section': sec, 'field': field}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'strategy_library')
    parser.add_argument('--dest', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--check', action='store_true', help='Validate exports against current source without writing')
    args = parser.parse_args(argv)
    try:
        if args.source.resolve() == args.dest.resolve():
            raise ValueError('Source and destination must be different directories')
        paths = sorted(args.source.glob('*_attack_strategy_*.md'))
        template = args.source / '_TEMPLATE.md'
        if template.exists():
            paths.append(template)
        if not paths:
            raise ValueError(f'No strategy Markdown files in {args.source}')
        outputs, entries = {}, []
        for path in paths:
            # Binary read preserves CRLF and prevents universal-newline rewriting.
            document = convert_document(path.read_bytes().decode('utf-8'), path.name)
            outputs[path.with_suffix('.json').name] = document
            entries.append({'id': document['id'], 'kind': document['kind'], 'file': path.with_suffix('.json').name,
                            'source_sha256': document['source']['sha256'],
                            'example_count': len(document['examples']), 'warnings': document['warnings']})
        outputs['strategy.schema.json'] = json_schema()
        outputs['index.json'] = {'schema_version': '1.0',
                                'strategy_count': sum(x['kind'] == 'strategy' for x in entries),
                                'example_count': sum(x['example_count'] for x in entries if x['kind'] == 'strategy'),
                                'entries': entries}
        serialized = {name: (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
                      for name, value in outputs.items()}
        # Preflight all files so a conflicting output cannot cause partial replacement.
        for name, payload in serialized.items():
            target = args.dest / name
            if args.check:
                if not target.is_file() or target.read_bytes() != payload:
                    raise ValueError(f'Missing or stale export: {target}')
            elif target.exists() and target.read_bytes() != payload:
                raise FileExistsError(f'Refusing to overwrite changed file: {target}; use a new destination')
        if not args.check:
            args.dest.mkdir(parents=True, exist_ok=True)
            for name, payload in serialized.items():
                target = args.dest / name
                if not target.exists():
                    target.write_bytes(payload)
        print(f'{"Verified" if args.check else "Exported"} {outputs["index.json"]["strategy_count"]} strategies, '
              f'{outputs["index.json"]["example_count"]} examples, template={template.exists()}')
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
