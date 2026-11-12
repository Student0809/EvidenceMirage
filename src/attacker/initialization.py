"""Strict, offline import boundary for historical route and first payload only."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .piminer import PUBLIC_FIELDS

IDENTITY_FIELDS = PUBLIC_FIELDS + ('step_index',)
TOP_FIELDS = {'schema_version', 'cases_path', 'cases_sha256', 'case_ids', 'initializations'}
ENTRY_FIELDS = {'case_id', 'identity', 'strategy_ids', 'strategy_paths', 'strategy_sha256',
                'route_sha256', 'payload', 'payload_sha256', 'analysis', 'analysis_sha256',
                'source_metadata'}
SOURCE_FIELDS = {'sample_index', 'sample_sha256'}
FRESH_SOURCE_FIELDS = {'kind', 'case_id', 'record_path', 'record_sha256'}


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON object key in initialization input')
        result[key] = value
    return result


def read_json(text):
    return json.loads(text, object_pairs_hook=_unique_object)


def local_path(root, path):
    if not isinstance(path, (str, Path)) or not str(path):
        raise ValueError('Initialization path must be nonempty')
    result = (Path(root).resolve() / path).resolve()
    if not result.is_relative_to(Path(root).resolve()):
        raise ValueError('Initialization paths must be inside this project')
    return result


def public_identity(case):
    if not isinstance(case, dict) or any(key not in case for key in IDENTITY_FIELDS):
        raise ValueError('Missing public initialization identity fields')
    for key in PUBLIC_FIELDS:
        if key == 'injection_candidates':
            if not isinstance(case[key], list) or not case[key]:
                raise ValueError('Missing public injection vectors')
        elif not isinstance(case[key], str) or not case[key]:
            raise ValueError('Missing public initialization identity text')
    if type(case['step_index']) is not int or case['step_index'] < 0:
        raise ValueError('Invalid public initialization step index')
    return {key: deepcopy(case[key]) for key in IDENTITY_FIELDS}


def route_digest(ids, paths, hashes):
    return sha256_bytes(canonical(list(zip(ids, paths, hashes))).encode('utf-8'))


def _check_hash(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError('Invalid initialization ' + label + ' SHA-256')


class InitializationBundle:
    """Load project-local, frozen inputs without reading the historical run.

    `for_case` returns a defensive copy and rechecks all imported bytes, so a
    strategy edit or replaced bundle cannot silently alter later generations.
    Historical feedback is excluded by exact field whitelists at this boundary.
    """
    def __init__(self, root, path):
        self.root = Path(root).resolve()
        self.path = local_path(self.root, path)
        data = self.path.read_bytes()
        bundle = read_json(data.decode('utf-8'))
        if (not isinstance(bundle, dict) or set(bundle) != TOP_FIELDS
                or type(bundle['schema_version']) is not int or bundle['schema_version'] not in (1, 2)):
            raise ValueError('Invalid initialization bundle schema or unexpected fields')
        self.schema_version = bundle['schema_version']
        self._input_hashes = {self.path: sha256_bytes(data)}
        cases_path = self._runtime_path(bundle['cases_path'])
        _check_hash(bundle['cases_sha256'], 'cases')
        self._remember(cases_path, bundle['cases_sha256'])
        cases = [read_json(line) for line in cases_path.read_text().splitlines() if line.strip()]
        ids = [case.get('case_id') for case in cases]
        if (not ids or any(not isinstance(cid, str) or not re.fullmatch(r'c[0-9]{3}', cid) for cid in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('Duplicate, missing, or invalid dataset case IDs')
        if ids != sorted(ids):
            raise ValueError('Dataset cases must retain original source order')
        if bundle['case_ids'] != ids:
            raise ValueError('Initialization case IDs must match dataset order exactly')
        entries = bundle['initializations']
        if (not isinstance(entries, list) or len(entries) != len(ids)
                or any(not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS for entry in entries)):
            raise ValueError('Invalid initialization entries or unexpected fields')
        if [entry['case_id'] for entry in entries] != ids:
            raise ValueError('Duplicate, missing, or out-of-order initialization cases')
        self._entries = {}
        for case, entry in zip(cases, entries):
            identity = public_identity(case)
            if canonical(entry['identity']) != canonical(identity):
                raise ValueError('Initialization public identity differs from dataset')
            self._validate_entry(entry)
            self._entries[entry['case_id']] = deepcopy(entry)
        self.input_paths = set(self._input_hashes)
        self.case_ids = tuple(ids)

    def _runtime_path(self, value):
        if (not isinstance(value, str) or not value or Path(value).is_absolute()
                or '..' in Path(value).parts or Path(value).as_posix() != value):
            raise ValueError('Runtime initialization paths must be project-relative')
        return local_path(self.root, value)

    def _remember(self, path, expected):
        if path in self._input_hashes and self._input_hashes[path] != expected:
            raise ValueError('Conflicting initialization file hashes')
        if sha256_bytes(path.read_bytes()) != expected:
            raise ValueError('Initialization input bytes changed')
        self._input_hashes[path] = expected

    def _validate_entry(self, entry):
        ids, paths, hashes = (entry[key] for key in
                              ('strategy_ids', 'strategy_paths', 'strategy_sha256'))
        if (not isinstance(ids, list) or not ids or len(ids) > 3
                or any(not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', sid) for sid in ids)
                or len(set(ids)) != len(ids) or not isinstance(paths, list)
                or not isinstance(hashes, list) or len(paths) != len(ids) or len(hashes) != len(ids)):
            raise ValueError('Invalid ordered initialization route')
        for sid, path, digest in zip(ids, paths, hashes):
            p = self._runtime_path(path)
            if p.name != sid + '.md':
                raise ValueError('Initialization strategy path does not match ordered ID')
            _check_hash(digest, 'strategy')
            self._remember(p, digest)
            # Validate text now; exact bytes remain in the file and frozen hash.
            p.read_bytes().decode('utf-8')
        if entry['route_sha256'] != route_digest(ids, paths, hashes):
            raise ValueError('Initialization route order or contents changed')
        for key in ('payload', 'analysis'):
            value = entry[key]
            if not isinstance(value, str) or (key == 'payload' and not value.strip()):
                raise ValueError('Invalid initialization ' + key)
            if sha256_bytes(value.encode('utf-8')) != entry[key + '_sha256']:
                raise ValueError('Initialization ' + key + ' bytes changed')
        metadata = entry['source_metadata']
        if self.schema_version == 2:
            if (not isinstance(metadata, dict) or set(metadata) != FRESH_SOURCE_FIELDS
                    or metadata['kind'] != 'fresh_public_generation'
                    or metadata['case_id'] != entry['case_id']):
                raise ValueError('Invalid fresh initialization provenance')
            _check_hash(metadata['record_sha256'], 'fresh generation record')
            path = self._runtime_path(metadata['record_path'])
            self._remember(path, metadata['record_sha256'])
            source = read_json(path.read_text())
            if (set(source) != {'schema_version','kind','case_id','identity','generated_at',
                                'target_executions','history','generation'}
                    or source['schema_version'] != 1 or source['kind'] != 'fresh_public_generation'
                    or source['case_id'] != entry['case_id'] or source['identity'] != entry['identity']
                    or source['target_executions'] != 0 or source['history'] != []):
                raise ValueError('Fresh initialization source contains incompatible history or identity')
            generation = source['generation']
            route = generation.get('routing', {})
            if (generation.get('status') != 'ready'
                    or generation.get('payload') != entry['payload']
                    or generation.get('payload_sha256') != entry['payload_sha256']
                    or generation.get('analysis') != entry['analysis']
                    or route.get('strategy_ids') != ids or route.get('strategy_paths') != paths
                    or route.get('strategy_sha256') != dict(zip(paths,hashes))):
                raise ValueError('Fresh initialization differs from its generation record')
            return
        if (not isinstance(metadata, dict) or set(metadata) != SOURCE_FIELDS
                or type(metadata['sample_index']) is not int or metadata['sample_index'] < 0
                or entry['case_id'] != f"c{metadata['sample_index']:03}"):
            raise ValueError('Invalid initialization source metadata or wrong case')
        _check_hash(metadata['sample_sha256'], 'source sample')

    def for_case(self, case):
        cid = case.get('case_id')
        if cid not in self._entries:
            raise ValueError('Case absent from initialization bundle')
        entry = self._entries[cid]
        if canonical(public_identity(case)) != canonical(entry['identity']):
            raise ValueError('Initialization public identity drift or wrong case')
        for path, digest in self._input_hashes.items():
            if sha256_bytes(path.read_bytes()) != digest:
                raise ValueError('Initialization input bytes changed after loading')
        return {key: deepcopy(entry[key]) for key in
                ('strategy_ids', 'strategy_paths', 'payload', 'analysis', 'source_metadata')}

    def strategy_document(self, case):
        route = self.for_case(case)
        paths = route['strategy_paths']
        return '\n\n'.join(
            f'===== Candidate strategy {i}/{len(paths)}: {Path(path).stem} =====\n\n'
            + (self.root / path).read_text()
            for i, path in enumerate(paths, 1))
