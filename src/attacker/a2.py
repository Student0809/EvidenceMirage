"""MiniPIM A2: unchanged methods, two selected original examples per routed strategy.

Retrieval is prepared offline and checked before any model call. The online
adapter keeps the original Router, role, black-box history, and Evidence policy
contract; it needs neither the source MiniPIM project nor an embedding runtime.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from .original import OriginalPIMinerAdapter
from .piminer import PUBLIC_FIELDS
from ..vendor import router
from ..vendor.a2_selection import TASK_TYPES, partition, select_cases
from ..vendor.a2_converter import convert_document
from .retrieval import PROTOCOL as V2_PROTOCOL, SELECTOR, select_cases_v2


PROTOCOL = 'minipim-a2-v1'


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def public_case_sha256(case):
    public = {key: case[key] for key in ('case_id', *PUBLIC_FIELDS)}
    text = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError('A2: ' + message)


class A2Resources:
    def __init__(self, root, library_path, retrieval_path):
        self.root = Path(root).resolve()
        self.library_path = self._local(library_path)
        self.retrieval_path = self._local(retrieval_path)
        self.library = json.loads(self.library_path.read_text())
        self.retrieval = json.loads(self.retrieval_path.read_text())
        self.library_sha256 = _digest(self.library_path)
        self.retrieval_sha256 = _digest(self.retrieval_path)
        _require(self.library.get('schema_version') == 1 and self.library.get('protocol') == PROTOCOL,
                 'unsupported library protocol')
        _require(self.retrieval.get('schema_version') == 1
                 and self.retrieval.get('protocol') in {PROTOCOL, V2_PROTOCOL},
                 'unsupported retrieval protocol')
        self.protocol = self.retrieval['protocol']
        if self.protocol == V2_PROTOCOL:
            method = self.retrieval.get('retrieval_method', {})
            _require(method.get('selector') == SELECTOR
                     and method.get('variant') in {'selector_only', 'field_embedding'},
                     'unsupported v2 retrieval method')
        _require(self.retrieval.get('library_sha256') == self.library_sha256,
                 'retrieval library hash mismatch; prepare A2 again')
        from ..engine.config import data_fingerprints
        self.input_paths = {self.library_path, self.retrieval_path}
        for resource in (self.library, self.retrieval):
            for relative, expected in data_fingerprints(resource.get('inputs_sha256', {})).items():
                path = self._local(relative)
                _require(_digest(path) == expected, 'prepared input changed: ' + relative)
                self.input_paths.add(path)
        dataset = self._local(self.retrieval['dataset_path'])
        _require(_digest(dataset) == self.retrieval.get('dataset_sha256'),
                 'prepared dataset changed; prepare A2 again')
        self.input_paths.add(dataset)
        sources = self.library['source_sha256']
        actual = {str(p.relative_to(self.root)) for p in
                  (self.root / 'strategy_library').glob('*_attack_strategy_*.md')}
        _require(set(sources) == actual and actual, 'source library differs from the Router library')
        self.strategies = self.library['strategies']
        _require(set(self.strategies) == {Path(p).stem for p in actual}, 'incomplete strategy registry')
        self.cases = self.library['cases']
        self.by_id = {case['case_id']: case for case in self.cases}
        _require(len(self.by_id) == len(self.cases), 'duplicate source case ID')
        # Repartition the current sources to verify that no method/example bytes
        # were rewritten or silently dropped while preparing the derived view.
        from ..vendor import a2_converter
        expected_ids = set()
        for relative, expected_hash in sources.items():
            path = self._local(relative)
            _require(_digest(path) == expected_hash, 'source library changed: ' + relative)
            self.input_paths.add(path)
            text = path.read_bytes().decode('utf-8')
            document = convert_document(text, path.name)
            segments, raw_cases = partition(document, a2_converter)
            strategy = self.strategies[path.stem]
            _require(strategy['id'] == path.stem and strategy['source'] == relative
                     and strategy['source_sha256'] == expected_hash, 'strategy provenance mismatch')
            method = ''.join(value for kind, value in segments if kind == 'method')
            _require(strategy['method'] == method, 'method text differs from source: ' + path.stem)
            dataset_name = 'agentdojo' if path.stem.startswith('agentdojo_') else 'ipi_arena'
            for ordinal, ((line, raw), example) in enumerate(zip(raw_cases, document['examples']), 1):
                identifier = f'{path.stem}::example-{ordinal:03d}'
                expected_ids.add(identifier)
                item = self.by_id.get(identifier, {})
                benchmark = example['benchmark_case']
                group = [dataset_name, benchmark['suite'], benchmark['user_task_id']]
                situation = (example['user_task'] or '') + '\n\n' + (example['placeholder_context'] or '')
                _require(item.get('strategy_id') == path.stem and item.get('source') == relative
                         and item.get('source_sha256') == expected_hash
                         and item.get('source_line') == line and item.get('raw') == raw,
                         'case text/provenance differs from source: ' + identifier)
                _require(item.get('group') == group and item.get('scenario') == benchmark['suite']
                         and item.get('situation') == situation, 'case situation/group mismatch: ' + identifier)
                _require(item.get('task_type') in TASK_TYPES and item.get('outcome_label') == 'unknown',
                         'invalid source label; archival outcomes must remain unknown')
        _require(set(self.by_id) == expected_ids, 'source cases missing or added')
        self._validated = {}

    def _local(self, path):
        _require(isinstance(path, (str, Path)), 'resource path required')
        result = (self.root / path).resolve()
        _require(result.is_relative_to(self.root), 'runtime resources must be inside this project')
        return result

    def validate_case(self, case):
        identifier = case['case_id']
        task = self.retrieval['tasks'].get(identifier)
        _require(task is not None, 'no prepared retrieval for case: ' + identifier)
        public_hash = public_case_sha256(case)
        _require(task.get('public_sha256') == public_hash,
                 'public case changed or stale retrieval: ' + identifier)
        if self._validated.get(identifier) == public_hash:
            return task
        _require(task.get('task_type') in TASK_TYPES and task.get('task_type_review'),
                 'reviewed task type required: ' + identifier)
        evidence = task.get('task_type_evidence')
        _require(isinstance(evidence, str) and evidence and evidence in case['user_task_prompt'],
                 'task type evidence must quote the public user task')
        scores = task.get('scores', {})
        _require(set(scores) == set(self.by_id)
                 and all(type(v) in (float, int) and math.isfinite(v) for v in scores.values()),
                 'complete finite source-case scores required')
        selections = task.get('selections', {})
        _require(set(selections) == set(self.strategies), 'selection cache must cover every Router strategy')
        for strategy_id in self.strategies:
            selector = select_cases_v2 if self.protocol == V2_PROTOCOL else select_cases
            selected, log = selector(self.cases, strategy_id=strategy_id,
                scenario=case['suite_name'], task_type=task['task_type'],
                current_group=['agentdojo', case['suite_name'], case['user_task_id']], scores=scores, k=2)
            stored = selections[strategy_id]
            _require(stored.get('case_ids') == [item['case_id'] for item in selected]
                     and stored.get('log') == log, 'selection cache differs from MiniPIM A2 rules')
        self._validated[identifier] = public_hash
        return task

    def compose(self, case, strategies):
        task = self.validate_case(case)
        ids = [item['id'] for item in strategies]
        _require(ids and len(ids) <= router.ROUTE_TOP_K and len(ids) == len(set(ids)), 'invalid route')
        docs, selections, full_paths = [], {}, []
        for i, item in enumerate(strategies, 1):
            identifier = item['id']
            _require(identifier in self.strategies, 'unknown routed strategy')
            strategy = self.strategies[identifier]
            path = self._local(strategy['source'])
            _require(Path(item['path']).resolve() == path, 'Router/source strategy path mismatch')
            full_paths.append(str(path))
            selection = task['selections'][identifier]
            selected = [self.by_id[cid] for cid in selection['case_ids']]
            text = strategy['method'] + '\n\n## Selected original examples\n\n' + '\n\n'.join(
                example['raw'] for example in selected)
            docs.append(f'===== Candidate strategy {i}/{len(ids)}: {identifier} =====\n\n{text}')
            selections[identifier] = deepcopy(selection)
        document = '\n\n'.join(docs)
        original = router._load_sample_strategy(self.root, dict(strategy_paths=full_paths), None)
        metadata = dict(protocol=self.protocol, library_sha256=self.library_sha256,
            retrieval_sha256=self.retrieval_sha256, public_sha256=task['public_sha256'],
            task_type=task['task_type'], selections=selections,
            strategy_document_sha256=hashlib.sha256(document.encode('utf-8')).hexdigest(),
            original_document_chars=len(original), a2_document_chars=len(document))
        if self.protocol == V2_PROTOCOL:
            metadata['retrieval_method'] = deepcopy(self.retrieval['retrieval_method'])
        return document, metadata


class A2PIMinerAdapter(OriginalPIMinerAdapter):
    mode = 'a2'

    def __init__(self, root, model, target_model, a2_library, a2_retrieval,
                 effort='max', timeout=900, decide=None, route_decide=None,
                 revision_profile='original'):
        self.resources = A2Resources(root, a2_library, a2_retrieval)
        super().__init__(root, model, target_model, effort, timeout, decide, route_decide,
                         revision_profile=revision_profile)

    def _validate_case(self, case):
        self.resources.validate_case(case)

    def _compose_strategy(self, case, strategies):
        document, metadata = self.resources.compose(case, strategies)
        return document, {'a2': metadata}
