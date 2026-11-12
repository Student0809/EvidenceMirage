#!/usr/bin/env python3
"""Prepare MiniPIM A2 resources with local CPU embeddings, no APIs or downloads.

For another dataset, supply reviewed --task-annotations, a new --retrieval path,
and --reuse-library. No existing output is overwritten. Annotation examples are
in data/a2/dev.annotations.json; each label needs a literal public-task quote.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.engine.config import data_fingerprints
from src.vendor import a2_converter
from src.vendor.a2_selection import TASK_TYPES, partition, select_cases

PROTOCOL = 'minipim-a2-v1'
PUBLIC_FIELDS = ('suite_name', 'user_task_id', 'injection_task_id', 'user_task_prompt',
                 'injection_task_goal', 'context_with_placeholder', 'injection_candidates')
DEFAULT_MODEL = Path('/mnt/data/users/zhengqingliu/Model/bge-base-en-v1.5')
MODEL_FILES = ('modules.json', 'config_sentence_transformers.json', 'config.json',
               'sentence_bert_config.json', 'model.safetensors', 'tokenizer.json',
               'tokenizer_config.json', 'special_tokens_map.json', 'vocab.txt',
               '1_Pooling/config.json')
PACKAGES = ('sentence-transformers', 'torch', 'transformers', 'numpy', 'tokenizers', 'safetensors')


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def public_sha256(task):
    return sha256(canonical({key: task[key] for key in ('case_id', *PUBLIC_FIELDS)}))


def relative(path):
    return str(Path(path).resolve().relative_to(ROOT.resolve()))


def read(path):
    return json.loads(Path(path).read_bytes())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def input_hashes(paths):
    by_name = {relative(path): path for path in sorted(set(map(Path, paths)))}
    return {name: file_hash(path) for name, path in data_fingerprints(by_name).items()}


def data_only_provenance(provenance):
    """Retain archival source locations without publishing Python source hashes."""
    result = deepcopy(provenance)
    for name, entry in result.get('upstream_files', {}).items():
        if Path(entry.get('snapshot', name)).suffix == '.py':
            entry.pop('sha256', None)
    for entry in result.get('copied_functions', {}).values():
        entry.pop('sha256', None)
    return result


def data_only_library(library):
    """Compare legacy libraries without requiring their obsolete code fingerprints."""
    result = deepcopy(library)
    result['inputs_sha256'] = data_fingerprints(result.get('inputs_sha256', {}))
    result['provenance']['upstream'] = data_only_provenance(result['provenance']['upstream'])
    return result


def verify_provenance():
    path = ROOT / 'strategy_library_a2/provenance.json'
    provenance = read(path)
    dependencies = [path, ROOT / 'requirements-a2.txt']
    for entry in provenance['upstream_files'].values():
        snapshot = ROOT / entry['snapshot']
        if snapshot.suffix == '.py':
            continue
        require(file_hash(snapshot) == entry['sha256'], 'Upstream provenance snapshot changed')
        dependencies.append(snapshot)
    entry = provenance['source_annotations']
    annotations_path = ROOT / entry['snapshot']
    require(file_hash(annotations_path) == entry['sha256'], 'Copied reviewed annotations changed')
    dependencies.append(annotations_path)
    return data_only_provenance(provenance), dependencies, read(annotations_path)


def reviewed_groups(annotations):
    require(annotations.get('schema_version') == 1 and annotations.get('task_types') == TASK_TYPES,
            'Annotation schema or task taxonomy differs from MiniPIM')
    labels = {}
    for label in annotations['labels']:
        group = tuple(label['group'])
        require(len(group) == 3 and all(isinstance(x, str) and x for x in group), 'Invalid group annotation')
        require(group not in labels, 'Duplicate reviewed group annotation: ' + repr(group))
        require(label['task_type'] in TASK_TYPES and isinstance(label['evidence'], str) and label['evidence'],
                'Missing task-type evidence')
        labels[group] = label
    return labels


def build_library(case_annotations):
    provenance, dependencies, source_annotations = verify_provenance()
    group_labels = reviewed_groups(source_annotations)
    reviews = read(case_annotations)
    require(reviews.get('schema_version') == 1, 'Unsupported case annotations')
    indexed = {r['case_id']: r for r in reviews['labels']}
    require(len(indexed) == len(reviews['labels']), 'Duplicate case review')
    paths = sorted((ROOT / 'strategy_library').glob('*_attack_strategy_*.md'))
    require(paths, 'No current strategy Markdown files')
    strategies, cases, warnings = {}, [], {}
    for path in paths:
        raw = path.read_bytes()
        document = a2_converter.convert_document(raw.decode('utf-8'), path.name)
        require(a2_converter.restore_markdown(document).encode('utf-8') == raw, 'Source bytes did not round-trip')
        segments, originals = partition(document, a2_converter)
        identifier, source = path.stem, relative(path)
        method = ''.join(text for kind, text in segments if kind == 'method')
        strategies[identifier] = dict(id=identifier, method=method, source=source, source_sha256=sha256(raw))
        warnings[identifier] = document['warnings']
        for ordinal, ((line, exact), example) in enumerate(zip(originals, document['examples']), 1):
            benchmark = example.get('benchmark_case')
            require(isinstance(benchmark, dict) and set(benchmark) == {'suite', 'user_task_id', 'injection_task_id'},
                    'Missing benchmark group in ' + identifier)
            dataset = 'agentdojo' if identifier.startswith('agentdojo_') else 'ipi_arena' if identifier.startswith('ipi_arena_') else None
            require(dataset is not None, 'Unknown strategy dataset: ' + identifier)
            group = [dataset, benchmark['suite'], benchmark['user_task_id']]
            case_id = f'{identifier}::example-{ordinal:03d}'
            require(case_id in indexed and tuple(group) in group_labels, 'Missing reviewed group/case: ' + case_id)
            review, upstream_label = indexed[case_id], group_labels[tuple(group)]
            user_task = example.get('user_task') or ''
            require(review['group'] == group and review['task_type'] == upstream_label['task_type'],
                    'Case review differs from registered group label: ' + case_id)
            require(review['source'] == source and review['source_line'] == line and review['source_sha256'] == sha256(raw),
                    'Case annotation source changed: ' + case_id)
            require(isinstance(review.get('reviewer'), str) and review['reviewer'].strip()
                    and isinstance(review.get('evidence'), str) and review['evidence'].strip()
                    and review['evidence'] in user_task, 'Case review is not a literal current task quote: ' + case_id)
            require(review.get('upstream_evidence_sha256') == sha256(upstream_label['evidence'].encode()),
                    'Case review does not bind upstream group evidence: ' + case_id)
            cases.append(dict(case_id=case_id, strategy_id=identifier, dataset=dataset, scenario=benchmark['suite'],
                group=group, task_type=review['task_type'], task_type_evidence=review['evidence'],
                task_type_review=review['reviewer'], outcome_label='unknown',
                outcome_evidence='Source has narrative result; no structured round-level verification.',
                user_task=user_task, situation=user_task + '\n\n' + (example.get('placeholder_context') or ''),
                raw=exact, source=source, source_line=line, source_sha256=sha256(raw)))
    require(set(indexed) == {c['case_id'] for c in cases}, 'Case annotations do not match current library')
    dependencies.extend([case_annotations, *paths])
    return dict(schema_version=1, protocol=PROTOCOL,
        source_sha256={relative(path): file_hash(path) for path in paths}, strategies=strategies, cases=cases,
        inputs_sha256=input_hashes(dependencies), provenance=dict(upstream=provenance, source_warnings=warnings,
            text_policy='MiniPIM exact partition: all method segments and selected raw cases unchanged; example-section preambles are not method.',
            review_policy='Copied upstream group labels revalidated using explicit literal current-source case reviews; all outcomes remain unknown.'))


def load_tasks(dataset, annotations, library):
    lines = dataset.read_bytes().decode('utf-8').splitlines()
    tasks = [(number, json.loads(line)) for number, line in enumerate(lines, 1) if line.strip()]
    require(tasks, 'Dataset is empty')
    reviewed = read(annotations)
    require(reviewed.get('schema_version') == 1 and reviewed.get('task_types') == TASK_TYPES, 'Task annotation taxonomy differs')
    labels = {r['case_id']: r for r in reviewed['labels']}
    require(len(labels) == len(reviewed['labels']), 'Duplicate task annotation')
    require(len({task['case_id'] for _, task in tasks}) == len(tasks), 'Duplicate dataset case ID')
    require(set(labels) == {task['case_id'] for _, task in tasks}, 'Missing or extra dataset task annotation')
    groups = {tuple(case['group']): case['task_type'] for case in library['cases']}
    result = []
    for line, task in tasks:
        require(all(key in task for key in ('case_id', *PUBLIC_FIELDS)), 'Missing public task fields')
        # This project runtime uses AgentDojo. Future datasets must carry a supported dataset_name explicitly.
        dataset_name = task.get('dataset_name', 'agentdojo')
        require(dataset_name in {'agentdojo', 'ipi_arena'}, 'Unknown task dataset')
        group = [dataset_name, task['suite_name'], task['user_task_id']]
        label = labels[task['case_id']]
        require(label.get('group') == group and label.get('task_type') in TASK_TYPES, 'Task group/type mismatch')
        require(label.get('public_sha256') == public_sha256(task), 'Task public fields changed')
        require(label.get('source') == relative(dataset) and label.get('source_line') == line
                and label.get('source_sha256') == file_hash(dataset), 'Task annotation dataset changed')
        require(isinstance(label.get('reviewer'), str) and label['reviewer'].strip()
                and isinstance(label.get('evidence'), str) and label['evidence'].strip()
                and label['evidence'] in task['user_task_prompt'], 'Task evidence must be a literal public user-task quote')
        require(tuple(group) not in groups or groups[tuple(group)] == label['task_type'], 'Inconsistent task-group labels')
        groups[tuple(group)] = label['task_type']
        result.append((task, group, label))
    return result


def embed(library, tasks, model_path):
    model_path = model_path.resolve()
    require(model_path.is_dir(), 'An existing local BGE model directory is required')
    require(all((model_path / name).is_file() for name in MODEL_FILES), 'Local BGE model files incomplete; downloads are disabled')
    # Fail closed to local files, including tokenizers and transformer submodules.
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1',
                      HF_HUB_DISABLE_TELEMETRY='1', TOKENIZERS_PARALLELISM='false', CUDA_VISIBLE_DEVICES='')
    versions = {package: importlib.metadata.version(package) for package in PACKAGES}
    hashes = {name: file_hash(model_path / name) for name in MODEL_FILES}
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer
    torch.set_num_threads(2)
    torch.manual_seed(0)
    encoder = SentenceTransformer(str(model_path), device='cpu', local_files_only=True,
                                  trust_remote_code=False, model_kwargs={'use_safetensors': True})
    require(encoder.max_seq_length == 512, 'A2 requires the original 512-token encoder limit')
    situations = [case['situation'] for case in library['cases']] + [
        task['user_task_prompt'] + '\n\n' + task['context_with_placeholder'] for task, _, _ in tasks]
    vectors = encoder.encode(situations, batch_size=8, normalize_embeddings=True, show_progress_bar=False)
    require(vectors.shape == (len(situations), 768) and np.isfinite(vectors).all(), 'Invalid BGE embedding matrix')
    # Preserve exact upstream per-row dot products rather than changing accumulation with a matrix operation.
    scores = [{case['case_id']: float(vectors[len(library['cases']) + index] @ vectors[i])
               for i, case in enumerate(library['cases'])} for index in range(len(tasks))]
    require(all(math.isfinite(score) for row in scores for score in row.values()), 'Non-finite similarity')
    require({name: file_hash(model_path / name) for name in MODEL_FILES} == hashes, 'Model files changed while encoding')
    metadata = dict(model='bge-base-en-v1.5', local_model_path=str(model_path), device='cpu', local_files_only=True,
        trust_remote_code=False, use_safetensors=True, model_sha256=hashes, packages=versions,
        query_prefix='', normalized=True, max_seq_length=encoder.max_seq_length, situation_only=True,
        similarity='dot product of normalized BGE vectors', embedding_dimension=768, batch_size=8,
        torch_threads=2, torch_seed=0, vector_dtype=str(vectors.dtype), vectors_sha256=sha256(vectors.tobytes()),
        input_sha256=[sha256(text.encode()) for text in situations],
        note='Encoder input limit applies to similarity only; prompt case text is never truncated.')
    return scores, metadata


def prepare(args):
    dataset, library_path, retrieval_path = (Path(x).resolve() for x in (args.dataset, args.library, args.retrieval))
    case_annotations, task_annotations = Path(args.case_annotations).resolve(), Path(args.task_annotations).resolve()
    for path in (dataset, library_path, retrieval_path, case_annotations, task_annotations):
        relative(path)
    require(library_path != retrieval_path, 'Library and retrieval outputs must differ')
    require(not retrieval_path.exists(), 'Refusing to overwrite retrieval output: ' + str(retrieval_path))
    require(args.reuse_library == library_path.exists(),
            'Existing library requires --reuse-library; a new library must not use it')
    library = build_library(case_annotations)
    library_raw = json_bytes(library)
    if args.reuse_library:
        existing_raw = library_path.read_bytes()
        require(data_only_library(json.loads(existing_raw)) == library,
                'Existing library is stale or differs; choose a new output path')
        # Preserve the original artifact and its data hash; never rewrite legacy resources.
        library_raw = existing_raw
    tasks = load_tasks(dataset, task_annotations, library)
    print(f'Encoding {len(library["cases"])} source situations and {len(tasks)} public tasks locally on CPU.', flush=True)
    scores, metadata = embed(library, tasks, Path(args.model))
    records = {}
    for (task, group, label), score in zip(tasks, scores):
        selections = {}
        for identifier in library['strategies']:
            selected, log = select_cases(library['cases'], strategy_id=identifier, scenario=task['suite_name'],
                task_type=label['task_type'], current_group=group, scores=score)
            selections[identifier] = dict(case_ids=[case['case_id'] for case in selected], log=log)
        records[task['case_id']] = dict(public_sha256=public_sha256(task), task_type=label['task_type'],
            task_type_evidence=label['evidence'], task_type_review=label['reviewer'], scores=score, selections=selections)
    retrieval = dict(schema_version=1, protocol=PROTOCOL, library_sha256=sha256(library_raw),
        dataset_sha256=file_hash(dataset), dataset_path=relative(dataset), embedding=metadata, tasks=records,
        inputs_sha256=input_hashes([dataset, task_annotations]))
    retrieval_raw = json_bytes(retrieval)
    # Detect concurrent data/resource edits before publishing immutable artifacts.
    for manifest in (library['inputs_sha256'], retrieval['inputs_sha256']):
        require(all(file_hash(ROOT / path) == digest for path, digest in manifest.items()), 'Inputs changed during preparation')
    require(build_library(case_annotations) == library, 'Library changed during preparation')
    if args.reuse_library:
        require(library_path.read_bytes() == library_raw, 'Existing library changed during preparation')
    if not args.reuse_library:
        library_path.parent.mkdir(parents=True, exist_ok=True)
        with library_path.open('xb') as handle:
            handle.write(library_raw)
    retrieval_path.parent.mkdir(parents=True, exist_ok=True)
    with retrieval_path.open('xb') as handle:
        handle.write(retrieval_raw)
    return dict(library=relative(library_path), retrieval=relative(retrieval_path),
                strategies=len(library['strategies']), cases=len(library['cases']), tasks=len(tasks),
                library_sha256=sha256(library_raw), retrieval_sha256=sha256(retrieval_raw))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT / 'data/dev.jsonl')
    parser.add_argument('--library', type=Path, default=ROOT / 'strategy_library_a2/library.json')
    parser.add_argument('--retrieval', type=Path, default=ROOT / 'data/a2/dev.retrieval.json')
    parser.add_argument('--case-annotations', type=Path, default=ROOT / 'strategy_library_a2/case_annotations.json')
    parser.add_argument('--task-annotations', type=Path, default=ROOT / 'data/a2/dev.annotations.json')
    parser.add_argument('--model', type=Path, default=DEFAULT_MODEL)
    parser.add_argument('--reuse-library', action='store_true', help='Verify and reuse an existing immutable library')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(prepare(args), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, ImportError) as error:
        parser.exit(2, f'A2 preparation failed: {error}\n')


if __name__ == '__main__':
    main()
