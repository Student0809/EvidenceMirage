#!/usr/bin/env python3
"""Create immutable, opt-in A2 retrieval ablations using local CPU only.

selector_only reuses every old BGE score; field_embedding changes only the
retrieval views and uses the same v2 selector. Neither rewrites library text.
"""
from copy import deepcopy
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import prepare_a2 as legacy
from src.attacker.a2 import A2Resources
from src.attacker.retrieval import (PROTOCOL, SELECTOR, FIELD_POLICY, build_embedding_fields,
                                    normalized_situation, select_cases_v2)


def selection_metrics(library, tasks, retrieval):
    by_id = {c['case_id']: c for c in library['cases']}
    metrics = dict(selection_sets=0, selected_examples=0, strict_available=0, strict_retained=0,
                   type_available=0, type_retained=0, duplicate_group_sets=0,
                   duplicate_situation_sets=0, shortfall_sets=0)
    for task in tasks:
        record = retrieval['tasks'][task['case_id']]
        for sid, selection in record['selections'].items():
            available = [c for c in library['cases'] if c['strategy_id'] == sid
                         and c['group'] != ['agentdojo', task['suite_name'], task['user_task_id']]]
            chosen = [by_id[cid] for cid in selection['case_ids']]
            same = lambda c: c['task_type'] == record['task_type']
            strict = lambda c: same(c) and c['scenario'] == task['suite_name']
            metrics['selection_sets'] += 1
            metrics['selected_examples'] += len(chosen)
            metrics['strict_available'] += any(strict(c) for c in available)
            metrics['strict_retained'] += any(strict(c) for c in chosen)
            metrics['type_available'] += any(same(c) for c in available)
            metrics['type_retained'] += any(same(c) for c in chosen)
            metrics['duplicate_group_sets'] += len({tuple(c['group']) for c in chosen}) < len(chosen)
            metrics['duplicate_situation_sets'] += len({normalized_situation(c['situation']) for c in chosen}) < len(chosen)
            metrics['shortfall_sets'] += len(chosen) < 2
    return metrics


def rebuild_retrieval(baseline, library, tasks, *, variant, scores=None, embedding=None, field_audit=None):
    if variant not in {'selector_only', 'field_embedding'}:
        raise ValueError('Unknown retrieval variant')
    if variant == 'field_embedding' and (scores is None or embedding is None):
        raise ValueError('field_embedding requires measured scores and embedding provenance')
    result = deepcopy(baseline)
    result['protocol'] = PROTOCOL
    result['retrieval_method'] = dict(selector=SELECTOR, variant=variant, goal_in_embedding=False,
        field_policy=FIELD_POLICY if variant == 'field_embedding' else None)
    if embedding is not None:
        result['embedding'] = embedding
    changed = []
    for task in tasks:
        record = result['tasks'][task['case_id']]
        if scores is not None:
            record['scores'] = scores[task['case_id']]
        for sid in library['strategies']:
            selected, log = select_cases_v2(library['cases'], strategy_id=sid,
                scenario=task['suite_name'], task_type=record['task_type'],
                current_group=['agentdojo', task['suite_name'], task['user_task_id']], scores=record['scores'])
            ids = [c['case_id'] for c in selected]
            previous = baseline['tasks'][task['case_id']]['selections'][sid]['case_ids']
            if ids != previous:
                changed.append(dict(case_id=task['case_id'], strategy_id=sid, old=previous, new=ids))
            record['selections'][sid] = dict(case_ids=ids, log=log)
    result['preparation_audit'] = dict(before=selection_metrics(library, tasks, baseline),
        after=selection_metrics(library, tasks, result), changed_selection_sets=len(changed),
        selection_changes=changed, field_audit=field_audit,
        limitation='Retrieval invariants and input visibility are development proxies, not relevance labels or attack success.')
    return result


def resolve_source_context(case, root=ROOT):
    """Expand an explicit local context link; never guess a historical context."""
    prefix = case['user_task'] + '\n\n'
    legacy.require(case['situation'].startswith(prefix), 'Invalid archived situation fields')
    context = case['situation'][len(prefix):]
    links = sorted(set(re.findall(r'strategy_library/contexts/[^`\s)]+\.json', context)))
    legacy.require(len(links) <= 1, 'Ambiguous archived context references')
    if not links:
        return context, {'context_resolution': 'archived_inline'}, None
    path = (root / links[0]).resolve()
    legacy.require(path.is_relative_to((root / 'strategy_library/contexts').resolve()), 'Context path escapes library')
    record = json.loads(path.read_text())
    legacy.require([case['dataset'], record['suite_name'], record['user_task_id']] == case['group'],
                   'Referenced context task group mismatch')
    heading_id = re.search(r'`([^`]+)`', case['raw'].splitlines()[0])
    expected = [part.strip() for part in heading_id[1].split('/')] if heading_id else []
    legacy.require(expected == [record['suite_name'], record['user_task_id'], record['injection_task_id']],
                   'Referenced context injection task mismatch')
    context = record['context_with_placeholder']
    legacy.require(isinstance(context, str) and bool(context), 'Referenced context is empty')
    legacy.require(legacy.sha256(context.encode()) == record['context_sha256'], 'Referenced context hash mismatch')
    return context, dict(context_resolution='linked_current_dataset', context_source=links[0],
                         context_source_sha256=legacy.file_hash(path), context_sha256=record['context_sha256']), path


def local_fields(library, tasks, model_path, *, resolve_sources=False):
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1',
        HF_HUB_DISABLE_TELEMETRY='1', TOKENIZERS_PARALLELISM='false', CUDA_VISIBLE_DEVICES='')
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True, trust_remote_code=False)
    rows, audit, references = {}, {}, set()
    for case in library['cases']:
        prefix = case['user_task'] + '\n\n'
        legacy.require(case['situation'].startswith(prefix), 'Invalid archived situation fields')
        if resolve_sources:
            context, provenance, path = resolve_source_context(case)
            if path is not None:
                references.add(path)
        else:
            context, provenance = case['situation'][len(prefix):], {'context_resolution': 'archived_inline'}
        rows[case['case_id']], audit[case['case_id']] = build_embedding_fields(case['user_task'], context, tokenizer)
        audit[case['case_id']].update(provenance)
        audit[case['case_id']]['archived_input_sha256'] = legacy.sha256(case['situation'].encode())
    for task in tasks:
        rows[task['case_id']], audit[task['case_id']] = build_embedding_fields(
            task['user_task_prompt'], task['context_with_placeholder'], tokenizer)
    return rows, audit, references


def field_scores(library, tasks, fields, model_path):
    legacy.require(all((model_path / name).is_file() for name in legacy.MODEL_FILES), 'Local BGE model incomplete')
    hashes = {name: legacy.file_hash(model_path / name) for name in legacy.MODEL_FILES}
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer
    torch.set_num_threads(2)
    torch.manual_seed(0)
    encoder = SentenceTransformer(str(model_path), device='cpu', local_files_only=True,
        trust_remote_code=False, model_kwargs={'use_safetensors': True})
    legacy.require(encoder.max_seq_length == 512, 'Expected the original 512-token BGE encoder')
    texts, spans = [], {}
    for cid, fields_by_name in fields.items():
        spans[cid] = {}
        for name, values in fields_by_name.items():
            start = len(texts)
            texts.extend(values)
            spans[cid][name] = (start, len(texts))
    vectors = encoder.encode(texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False)
    legacy.require(np.isfinite(vectors).all() and vectors.shape == (len(texts), 768), 'Invalid field vectors')
    pooled = {}
    for cid, names in spans.items():
        pooled[cid] = {}
        for name, (start, end) in names.items():
            vector = vectors[start:end].mean(axis=0)
            norm = float(np.linalg.norm(vector))
            legacy.require(norm > 0 and np.isfinite(norm), 'Invalid pooled field vector')
            pooled[cid][name] = vector / norm
    scores = {task['case_id']: {case['case_id']: float(sum(
        .5 * (pooled[task['case_id']][name] @ pooled[case['case_id']][name])
        for name in ('task', 'context'))) for case in library['cases']} for task in tasks}
    legacy.require({name: legacy.file_hash(model_path / name) for name in legacy.MODEL_FILES} == hashes,
                   'Model changed during preparation')
    metadata = dict(model='bge-base-en-v1.5', local_model_path=str(model_path), device='cpu',
        local_files_only=True, trust_remote_code=False, use_safetensors=True, model_sha256=hashes,
        packages={name: importlib.metadata.version(name) for name in legacy.PACKAGES},
        normalized=True, query_prefix='', max_seq_length=512, embedding_dimension=768,
        batch_size=8, torch_threads=2, torch_seed=0, vector_dtype=str(vectors.dtype),
        vectors_sha256=legacy.sha256(vectors.tobytes()), input_sha256=[legacy.sha256(t.encode()) for t in texts],
        field_policy=FIELD_POLICY, task_token_budget=192, context_token_budget=320,
        field_weights={'task': .5, 'context': .5}, context_pooling='unit-normalized mean of slot windows',
        similarity='equal-weight sum of normalized task and context dot products',
        situation_only=True, goal_in_embedding=False,
        note='Field excerpts affect retrieval only; method and selected raw case text stay unchanged.')
    return scores, metadata


def prepare(args):
    output = args.retrieval.resolve()
    legacy.relative(output)
    legacy.require(not output.exists(), 'Refusing to overwrite retrieval output')
    resources = A2Resources(ROOT, args.library, args.baseline_retrieval)
    baseline, library = resources.retrieval, resources.library
    legacy.require(baseline['protocol'] == 'minipim-a2-v1', 'Baseline must be an unchanged v1 retrieval')
    tasks = [json.loads(line) for line in (ROOT / baseline['dataset_path']).read_text().splitlines() if line.strip()]
    for task in tasks:
        resources.validate_case(task)
    model_path = (args.model or Path(baseline['embedding']['local_model_path'])).resolve()
    fields, audit, references = local_fields(library, tasks, model_path,
                                            resolve_sources=args.variant == 'field_embedding')
    scores, embedding = (field_scores(library, tasks, fields, model_path)
                         if args.variant == 'field_embedding' else (None, None))
    result = rebuild_retrieval(baseline, library, tasks, variant=args.variant,
                              scores=scores, embedding=embedding, field_audit=audit)
    result['inputs_sha256'] = legacy.input_hashes(resources.input_paths | references)
    result['baseline_retrieval'] = dict(path=legacy.relative(resources.retrieval_path),
                                      sha256=resources.retrieval_sha256)
    for relative, expected in result['inputs_sha256'].items():
        legacy.require(legacy.file_hash(ROOT / relative) == expected, 'Input changed during preparation')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as handle:
        handle.write(legacy.json_bytes(result))
    checked = A2Resources(ROOT, args.library, output)
    for task in tasks:
        checked.validate_case(task)
    return dict(retrieval=legacy.relative(output), sha256=legacy.file_hash(output), variant=args.variant,
                changed_selection_sets=result['preparation_audit']['changed_selection_sets'],
                before=result['preparation_audit']['before'], after=result['preparation_audit']['after'],
                source_cases=len(library['cases']), tasks=len(tasks), api_calls=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, default=ROOT / 'strategy_library_a2/library.json')
    parser.add_argument('--baseline-retrieval', type=Path, default=ROOT / 'data/a2/dev.retrieval.json')
    parser.add_argument('--variant', choices=('selector_only', 'field_embedding'), required=True)
    parser.add_argument('--retrieval', type=Path, required=True)
    parser.add_argument('--model', type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
