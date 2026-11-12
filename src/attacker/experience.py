"""Committed rolling experience for one run and one policy.

The caller owns the run/policy directory boundary. Nothing is imported from
historical runs or sibling policy directories. A separate lock serializes both
the duplicate check and atomic publication; snapshots observe committed entries
only. Original PIMiner helpers retain the summary and rendering semantics.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from ..vendor.experience import (
    INTRA_EXPERIENCE_FILE, _append_intra_experience,
    _format_intra_experience_block,
)


ROLLING_CUTOFF = 1 << 30
EXPERIENCE_CHAR_BUDGET = 20000
HISTORY_FIELDS = ('iter', 'analysis', 'injection', 'security', 'utility', 'final_output')
PUBLIC_CASE_FIELDS = ('suite_name', 'user_task_id', 'injection_task_id',
                      'user_task_prompt', 'injection_task_goal', 'context_with_placeholder')


def _case_index(case):
    """Use an explicit original index when available, otherwise the cNNN ID."""
    source = case.get('provenance', {}).get('source_metadata', {})
    values = [value for value in (case.get('sample_index'), case.get('source_sample_index'),
                                 source.get('source_sample_index')) if value is not None]
    match = re.fullmatch(r'c(\d+)', case['case_id'])
    if match:
        values.append(int(match.group(1)))
    if (not values or any(type(value) is not int or not 0 <= value < ROLLING_CUTOFF
                          for value in values) or len(set(values)) != 1):
        raise ValueError('Missing, invalid, or conflicting experience sample index')
    return values[0]


def _validate_case_id(case_id):
    if not isinstance(case_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', case_id):
        raise ValueError('Invalid experience case ID')


def _serialize(entries):
    return ''.join(json.dumps(entry, ensure_ascii=False, allow_nan=False) + '\n'
                   for entry in entries)


class RollingExperience:
    """One store shared by workers of the same run and policy.

    Call ``publish`` only from the terminal-case hook. Its outcome must be the
    Runner's valid terminal outcome; pending/error/unknown outcomes are ignored.
    The first publication binds the directory to an outcome policy ID, preventing
    accidental mixed-policy publication even if a caller reuses the wrong path.
    """

    def __init__(self, directory, enabled=True):
        if type(enabled) is not bool:
            raise ValueError('Rolling experience enabled must be boolean')
        self.enabled = enabled
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / INTRA_EXPERIENCE_FILE
        self.lock_path = self.directory / '.experience.lock'

    @contextmanager
    def _locked(self, exclusive):
        # NFS shared locks require a readable descriptor; both lock modes use a+.
        with self.lock_path.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read_entries(self):
        if not self.path.exists():
            return []
        entries, case_ids, indices, policies = [], set(), set(), set()
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            # Fail closed on a partial/corrupt file; never promote an incomplete
            # publication or silently replace evidence with an empty store.
            entry = json.loads(line)
            if (not isinstance(entry, dict) or entry.get('_schema_version') != 1
                    or entry.get('outcome') not in ('hit', 'miss')
                    or type(entry.get('success')) is not bool
                    or entry['success'] != (entry['outcome'] == 'hit')
                    or entry.get('_terminal_status') != ('VALID_GOLD_TRUE' if entry['success'] else 'VALID_GOLD_FALSE')
                    or type(entry.get('sample_index')) is not int
                    or not 0 <= entry['sample_index'] < ROLLING_CUTOFF
                    or type(entry.get('iters_used')) is not int or entry['iters_used'] < 1
                    or not isinstance(entry.get('_policy_id'), str) or not entry['_policy_id']):
                raise ValueError('Invalid or uncommitted experience entry')
            _validate_case_id(entry.get('_case_id'))
            if entry['_case_id'] in case_ids or entry['sample_index'] in indices:
                raise ValueError('Duplicate committed experience identity')
            case_ids.add(entry['_case_id'])
            indices.add(entry['sample_index'])
            policies.add(entry['_policy_id'])
            entries.append(entry)
        if len(policies) > 1:
            raise ValueError('Experience directory contains multiple policies')
        return entries

    def _replace(self, entries):
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                    prefix='.experience-', suffix='.tmp', dir=self.directory, delete=False) as stream:
                temp_path = Path(stream.name)
                stream.write(_serialize(entries))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def snapshot(self, case_id):
        """Return original rolling text and provenance for a committed snapshot.

        Higher-index siblings that have already finished are visible, as in the
        original rolling scheduler. Unpublished future results are never read.
        The current case is excluded even if the caller asks after publishing it.
        """
        _validate_case_id(case_id)
        with self._locked(exclusive=False):
            all_entries = self._read_entries() if self.enabled else []
            entries = [entry for entry in all_entries if entry['_case_id'] != case_id]
            entries.sort(key=lambda entry: entry['sample_index'])
            serialized = _serialize(entries)
        # Render an immutable, filtered copy using the verbatim file-based helper.
        # This keeps the original soft budget and does not mutate the shared log.
        with tempfile.TemporaryDirectory(prefix='pim-experience-snapshot-') as directory:
            view = Path(directory)
            (view / INTRA_EXPERIENCE_FILE).write_text(serialized)
            text = _format_intra_experience_block(view, ROLLING_CUTOFF,
                                                  EXPERIENCE_CHAR_BUDGET)
        metadata = dict(source=str(self.path), directory=str(self.directory),
            policy_id=all_entries[0]['_policy_id'] if all_entries else None,
            excluded_case_id=case_id, entries=len(entries),
            case_ids=[entry['_case_id'] for entry in entries],
            case_ids_scope='committed_entries_before_original_soft_truncation',
            before_index=ROLLING_CUTOFF, max_chars=EXPERIENCE_CHAR_BUDGET,
            snapshot_sha256=hashlib.sha256(serialized.encode()).hexdigest(),
            text_sha256=hashlib.sha256(text.encode()).hexdigest())
        if not self.enabled:
            metadata['enabled'] = False
        return text, metadata

    def publish(self, case, history, outcome, strategy_ids):
        """Publish once for a valid terminal case; return whether a row was added."""
        if not self.enabled:
            return False
        success = outcome.get('gold_success')
        status = outcome.get('status')
        if (type(success) is not bool
                or status != ('VALID_GOLD_TRUE' if success else 'VALID_GOLD_FALSE')):
            return False
        case_id = case['case_id']
        _validate_case_id(case_id)
        if outcome.get('case_id', case_id) != case_id:
            raise ValueError('Experience outcome case differs')
        policy_id = outcome.get('policy_id')
        if not isinstance(policy_id, str) or not policy_id:
            raise ValueError('Experience publication requires a policy ID')
        if (not history or any(type(item.get('security')) is not bool for item in history)
                or any(not isinstance(item.get('injection'), str) or not item['injection'] for item in history)):
            return False
        if (any(item['security'] for item in history) != success
                or history[-1]['security'] is not success):
            raise ValueError('Experience outcome does not match completed history')
        if any(item.get('iter') != index or type(item.get('iter')) is not int
               for index, item in enumerate(history)):
            raise ValueError('Experience history must use consecutive attempt indices')
        if ('target_attempts' in outcome and outcome['target_attempts'] != len(history)-1):
            raise ValueError('Experience outcome budget does not match history')
        if (not isinstance(strategy_ids, (list, tuple))
                or any(not isinstance(value, str) for value in strategy_ids)):
            raise ValueError('Invalid experience strategy IDs')
        sample = {key: deepcopy(case.get(key)) for key in PUBLIC_CASE_FIELDS}
        sample.update(sample_index=_case_index(case), status='hit' if success else 'miss',
            history=[{key: deepcopy(item.get(key)) for key in HISTORY_FIELDS} for item in history],
            strategy_ids=list(strategy_ids))
        # The helper creates precisely the historical summary schema. Its old
        # check-before-lock behavior is safe only on this private temporary file.
        with tempfile.TemporaryDirectory(prefix='pim-experience-entry-') as directory:
            view = Path(directory)
            _append_intra_experience(view, sample)
            entry = json.loads((view / INTRA_EXPERIENCE_FILE).read_text())
        entry.update(_schema_version=1, _case_id=case_id, _policy_id=policy_id,
                     _terminal_status=status)
        with self._locked(exclusive=True):
            entries = self._read_entries()
            if entries and entries[0]['_policy_id'] != policy_id:
                raise ValueError('Experience directory belongs to another policy')
            if any(old['_case_id'] == case_id for old in entries):
                return False
            if any(old['sample_index'] == entry['sample_index'] for old in entries):
                raise ValueError('Experience index already belongs to another case')
            self._replace(entries + [entry])
        return True
