"""Explicit recovery of the completed P0 prefix of the NFS-lock failure run.

This is deliberately not a general resume mechanism. It accepts only a complete
aligned run whose first revision failed before any model request or publication.
Every old Target execution is accounted once and consumes the original budget.
"""
from copy import deepcopy
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path

from .budget import Budget
from .config import ROOT, data_fingerprints, load_cases
from .history import History
from .runner import Runner
from .storage import plain
from ..attacker.initialization import InitializationBundle, canonical, read_json
from ..evidence.ledger import EvidenceLedger
from ..evidence.stage import estimate_stage, response_signal
from ..policies.registry import build_policies
from ..schema import Action, Episode, Payload, TargetResult

ORIGIN = 'recovered_current_run_prefix'


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError('Unsafe prefix recovery: ' + message)


class RecoveryPrefix:
    def __init__(self, source_run, current_manifest):
        self.source_run = Path(source_run).resolve()
        _require(self.source_run.is_dir(), 'source run does not exist')
        self._hashes, self._raw_bytes, self._records, self._cases = {}, {}, {}, {}
        self._raw_files = {}
        source = self._read('manifest.json')
        config = current_manifest['config']
        _require(source['config'] == config, 'source/current configuration differs')
        _require(source['environment'] == current_manifest['environment'], 'runtime environment differs')
        _require(config['attacker'].get('mode') == 'piminer_aligned'
                 and config['backend'] == 'agentdojo'
                 and set(config['policies']) == {'stage_aware'}, 'unsupported protocol')
        self.config = deepcopy(config)
        cases = load_cases(config)
        ids = [case['case_id'] for case in cases]
        _require(source['case_ids'] == current_manifest['case_ids'] == ids
                 and source.get('cases') == cases, 'source/current cases differ')
        # Legacy manifests may contain Python fingerprints. Code changes do not
        # gate recovery; dataset, strategy and other resource identities still do.
        old_inputs = data_fingerprints(source['inputs_sha256'])
        new_inputs = data_fingerprints(current_manifest['inputs_sha256'])
        _require(old_inputs == new_inputs, 'data/resource fingerprints differ')
        status = self._read('status.json')
        _require(status.get('status') == 'completed'
                 and status.get('selected_cases') == status.get('completed_workers') == len(cases),
                 'parent run is not fully completed')
        workers = self._read('worker_results.json')
        _require(isinstance(workers, list) and len(workers) == len(cases)
                 and sorted(row.get('case_id', '') for row in workers) == sorted(ids)
                 and all(row.get('status') == 'completed' for row in workers),
                 'source workers are not completely accounted')
        workers = {row['case_id']: row for row in workers}
        preflight = self._read('preflight.json')
        _require(isinstance(preflight, dict) and set(preflight) == {'native', 'target', 'attacker'},
                 'source preflight record is missing or malformed')
        _require(all(preflight[name].get('status') == 'passed' for name in ('native', 'target', 'attacker'))
                 and len(preflight['target'].get('calls', [])) == 1
                 and len(preflight['attacker'].get('attacker', {}).get('calls', [])) == 2,
                 'source model preflight did not complete exactly three calls')
        _require(source.get('limits', {}).get('preflight_model_calls') == 3,
                 'unexpected source preflight accounting')
        bundle = InitializationBundle(ROOT, config['attacker']['initialization_manifest'])
        for case in cases:
            cid = case['case_id']
            self._cases[cid] = deepcopy(case)
            self._records[cid] = self._case_prefix(case, source, workers[cid], bundle.for_case(case))
        self._assert_no_sessions_or_memory()
        counts = {cid: len(records) for cid, records in self._records.items()}
        self.metadata = dict(record_origin=ORIGIN, source_run=str(self.source_run),
            prefix_target_executions=sum(counts.values()), case_prefix_counts=counts,
            source_manifest_sha256=self._hashes['manifest.json'],
            source_preflight=dict(path='preflight.json', sha256=self._hashes['preflight.json'],
                                  model_calls=3, reused=True),
            reason='resume_after_pre_request_nfs_lock_oserror',
            source_snapshot_sha256=_hash(canonical(self._hashes).encode('utf-8')))

    def _path(self, relative):
        _require(isinstance(relative, str) and not Path(relative).is_absolute()
                 and '..' not in Path(relative).parts, 'unsafe source record path')
        path = (self.source_run / relative).resolve()
        _require(path.is_relative_to(self.source_run) and path.is_file(), 'missing or escaped source record')
        return path

    def _read(self, relative, *, lines=False):
        data = self._path(relative).read_bytes()
        self._hashes[relative] = _hash(data)
        if lines:
            return [read_json(line) for line in data.decode('utf-8').splitlines() if line.strip()]
        return read_json(data.decode('utf-8'))

    def _case_prefix(self, case, source, worker, initial):
        cid = case['case_id']
        child = self._read(cid + '/manifest.json')
        _require(child.get('config') == source['config']
                 and data_fingerprints(child.get('inputs_sha256', {})) == data_fingerprints(source['inputs_sha256'])
                 and child.get('environment') == source['environment']
                 and child.get('cases') == [case] and child.get('case_ids') == [cid]
                 and Path(child.get('parent_run', '')).resolve() == self.source_run,
                 'child manifest differs from parent')
        _require(self._read(cid + '/status.json').get('status') == 'completed', 'child is not completed')
        events = self._read(cid + '/events.jsonl', lines=True)
        episodes = self._read(cid + '/episodes.jsonl', lines=True)
        decisions = self._read(cid + '/decisions.jsonl', lines=True)
        outcomes = self._read(cid + '/outcomes.json')
        _require(1 <= len(episodes) <= self.config['post_failure_target_budget'] + 1,
                 'empty or over-budget prefix')
        expected_outcome = dict(case_id=cid, policy_id='stage_aware', initial_gold=False,
            gold_success=None, status='GENERATION_ERROR', target_attempts=len(episodes)-1,
            shared_h1_id=cid + '/shared/000', revisions=1)
        _require(outcomes == [expected_outcome] and worker.get('outcomes') == outcomes
                 and worker.get('target_executions') == len(episodes), 'source outcome is not first-revision failure')
        p0_path, p1_path = (f'{cid}/raw/{cid}/shared/P0.generation.json',
                            f'{cid}/raw/{cid}/stage_aware/P1.generation.json')
        p0, p1 = self._read(p0_path), self._read(p1_path)
        payload = Payload('P0', initial['payload'])
        _require(p0.get('status') == 'ready' and p0.get('mode') == 'piminer_aligned'
                 and p0.get('calls') == [] and p0.get('candidates') == []
                 and p0.get('payload') == payload.text and p0.get('payload_sha256') == payload.sha256
                 and p0.get('analysis') == initial['analysis']
                 and p0.get('generation_id') == cid + '/shared/P0'
                 and p0.get('reused_target_feedback') is False
                 and p0.get('routing', {}).get('strategy_ids') == initial['strategy_ids']
                 and p0.get('routing', {}).get('strategy_paths') == initial['strategy_paths'],
                 'source initialization is not the fixed imported P0')
        _require(p1.get('status') == 'GENERATION_ERROR' and p1.get('error_type') == 'OSError'
                 and p1.get('mode') == 'piminer_aligned' and p1.get('calls') == []
                 and p1.get('candidates') == [] and p1.get('checks') == []
                 and p1.get('generation_id') == cid + '/stage_aware/P1'
                 and p1.get('case_id') == cid and p1.get('policy_id') == 'stage_aware'
                 and not any(k in p1 for k in ('payload', 'analysis', 'session_id', 'experience')),
                 'failure is not the supported pre-request OSError')
        expected_events = [dict(event='GENERATION_STARTED', generation_id=cid + '/shared/P0'),
            dict(event='GENERATION_COMPLETED', generation_id=cid + '/shared/P0', status='ready',
                 raw_path=f'raw/{cid}/shared/P0.generation.json')]
        records = []
        raw_files = {p0_path, p1_path}
        for ordinal, episode in enumerate(episodes):
            policy = 'shared' if ordinal == 0 else 'stage_aware'
            eid = f'{cid}/{policy}/{ordinal:03d}'
            raw_relative = 'raw/' + eid + '.json'
            _require(set(episode) == {field.name for field in fields(Episode)}
                     and episode['episode_id'] == eid and episode['case_id'] == cid
                     and episode['policy_id'] == policy and episode['payload_id'] == 'P0'
                     and episode['payload_sha256'] == payload.sha256
                     and episode['phase'] == ('initial' if ordinal == 0 else 'post_failure')
                     and episode['status'] == 'VALID_GOLD_FALSE' and episode['gold'] is False
                     and episode['error'] is None and episode['raw_path'] == raw_relative,
                     'source has non-prefix, invalid, or successful Target episode')
            source_raw = cid + '/' + raw_relative
            raw = self._read(source_raw)
            _require(set(raw) == {'payload', 'result', 'query_packets'} and raw['payload'] == payload.text,
                     'source raw payload differs')
            result = TargetResult(**raw['result'])
            _require(result.gold is False and result.error is None, 'source raw Target result is not a valid failure')
            packets = raw['query_packets']
            _require(isinstance(packets, list), 'source query packets are malformed')
            ledger = EvidenceLedger(eid, case['goal_spec'])
            for packet in packets:
                _require(packet.get('episode_id') == eid, 'source packet belongs to another episode')
                ledger.update(packet)
            rebuilt = Episode(eid, cid, policy, 'P0', payload.sha256, episode['phase'],
                'VALID_GOLD_FALSE', False, result.final, ledger.trigger,
                estimate_stage(False, ledger.trigger, None), response_signal(result.final),
                ledger.records(), result.observation, result.usage, None, result.observation_error,
                result.seconds, sum(packet['charged_cost'] for packet in packets), raw_relative)
            _require(plain(rebuilt) == episode, 'source episode does not match its raw result and packets')
            expected_events.extend([dict(event='TARGET_STARTED', episode_id=eid, payload_sha256=payload.sha256),
                                    dict(event='TARGET_COMPLETED', episode_id=eid, status='VALID_GOLD_FALSE')])
            self._raw_bytes[source_raw] = self._path(source_raw).read_bytes()
            records.append(dict(episode=episode, raw=raw, source_raw_sha256=self._hashes[source_raw],
                source_raw_path=raw_relative, analysis=initial['analysis'], source_run=str(self.source_run),
                source_episode_id=eid))
            raw_files.add(source_raw)
        expected_events.extend([dict(event='GENERATION_STARTED', generation_id=cid + '/stage_aware/P1'),
            dict(event='GENERATION_COMPLETED', generation_id=cid + '/stage_aware/P1',
                 status='GENERATION_ERROR', raw_path=f'raw/{cid}/stage_aware/P1.generation.json'),
            dict(event='EXPERIENCE_COMMITTED', case_id=cid, policy_id='stage_aware', published=False,
                 path=str(self.source_run / 'experience/stage_aware/intra_dataset_experience.jsonl'))])
        _require(events == expected_events, 'source has unknown, incomplete, duplicated, or model-started events')
        actual_raw = {str(path.relative_to(self.source_run)) for path in
                      (self.source_run / cid / 'raw').rglob('*') if path.is_file()}
        _require(actual_raw == raw_files, 'source contains unaccounted raw executions or generations')
        self._raw_files[cid] = raw_files
        self._validate_decisions(case, episodes, decisions)
        return records

    def _validate_decisions(self, case, episodes, decisions):
        policy = build_policies(self.config['policies'])['stage_aware']
        budget = Budget(self.config['post_failure_target_budget'])
        expected = []
        for i in range(len(episodes)):
            history = [Episode(**row) for row in episodes[:i+1]]
            visible = History.from_episodes(history, budget)
            decision = policy.decide(visible)
            _require(decision.action == (Action.RETRY if i < len(episodes)-1 else Action.REVISE),
                     'source prefix does not stop at the first current-policy revision')
            row = dict(decision_id=f"{case['case_id']}/stage_aware/d{i+1:03d}",
                case_id=case['case_id'], policy_id='stage_aware',
                visible_history_ids=[item.episode_id for item in history], current_stage=history[-1].stage,
                history=visible.summary(), remaining_target_budget=budget.remaining,
                spent_target_budget=budget.spent, action=decision.action.value, reason=decision.reason)
            expected.append(row)
            if i < len(episodes)-1:
                budget.reserve()
        expected.append(dict(expected[-1], decision_id=expected[-1]['decision_id'] + '/error',
                             action='Stop', reason='GENERATION_ERROR'))
        _require(decisions == expected, 'source policy decisions differ or are incomplete')

    def _assert_no_sessions_or_memory(self):
        _require(not any(path.name == 'sessions' or (path.is_file() and 'sessions' in path.parts)
                         for path in self.source_run.rglob('*')),
                 'source contains a model session')
        directory = self.source_run / 'experience'
        if directory.exists():
            for path in directory.rglob('*'):
                if path.is_file():
                    _require(path.name == '.experience.lock' and path.stat().st_size == 0,
                             'source contains published or uncertain memory')

    def assert_immutable(self, case_id=None):
        for relative, digest in self._hashes.items():
            if case_id is None or '/' not in relative or relative.startswith(case_id + '/'):
                _require(_hash(self._path(relative).read_bytes()) == digest, 'source record bytes changed')
        for cid, expected in self._raw_files.items():
            if case_id is None or cid == case_id:
                actual = {str(path.relative_to(self.source_run)) for path in
                          (self.source_run / cid / 'raw').rglob('*') if path.is_file()}
                _require(actual == expected, 'source raw record set changed')
        self._assert_no_sessions_or_memory()

    def for_case(self, case):
        cid = case.get('case_id')
        _require(cid in self._records and canonical(case) == canonical(self._cases[cid]),
                 'unknown case or case identity drift')
        self.assert_immutable(cid)
        return deepcopy(self._records[cid])


class _PrefixPolicy:
    def __init__(self, policy, runner):
        self.policy, self.runner = policy, runner

    def decide(self, history):
        decision = self.policy.decide(history)
        cid = history.last.episode_id.split('/')[0]
        runner = self.runner
        if not runner._continued.get(cid):
            pending = runner._positions[cid] < len(runner._prefixes[cid])
            expected = Action.RETRY if pending else Action.REVISE
            _require(decision.action == expected, 'policy diverged before complete prefix recovery')
        return decision


class PrefixRecoveryRunner(Runner):
    def __init__(self, *args, recovery, **kwargs):
        _require(isinstance(recovery, RecoveryPrefix), 'a validated RecoveryPrefix is required')
        self.recovery = recovery
        self._prefixes, self._positions, self._continued = {}, {}, {}
        self._recovered_count = 0
        super().__init__(*args, **kwargs)
        _require(getattr(self.attacker, 'mode', None) == 'piminer_aligned'
                 and set(self.policies) == {'stage_aware'}
                 and self.limit == recovery.config['post_failure_target_budget']
                 and self.seed == recovery.config['seed'], 'runner does not match the recovery protocol')
        self.policies = {key: _PrefixPolicy(policy, self) for key, policy in self.policies.items()}

    def _case(self, case):
        cid = case['case_id']
        _require(cid not in self._prefixes, 'case cannot be replayed twice')
        self._prefixes[cid] = self.recovery.for_case(case)
        self._positions[cid] = 0
        self._continued[cid] = False
        super()._case(case)
        _require(self._positions[cid] == len(self._prefixes[cid]), 'case ended before consuming its complete prefix')

    def generate(self, case, policy_id, payload, last, revision_id, history=()):
        cid = case['case_id']
        prefix = self._prefixes[cid]
        self.recovery.assert_immutable(cid)
        if payload is not None and not self._continued[cid]:
            _require(self._positions[cid] == len(prefix), 'cannot revise before recovering the full prefix')
            _require(policy_id == 'stage_aware' and revision_id == 'P1'
                     and payload.id == 'P0' and payload.text == prefix[0]['raw']['payload']
                     and [plain(episode) for episode in history] == [record['episode'] for record in prefix],
                     'first resumed revision does not use the complete original prefix')
            self._continued[cid] = True
        result, record = super().generate(case, policy_id, payload, last, revision_id, history)
        if payload is None:
            _require(policy_id == 'shared' and revision_id == 'P0' and result is not None
                     and result.text == prefix[0]['raw']['payload'] and result.id == 'P0'
                     and record.get('analysis') == prefix[0]['analysis'] and record.get('calls') == [],
                     'recovery must import the same P0 without a model call')
        return result, record

    def execute(self, case, policy_id, payload, phase, ordinal):
        cid = case['case_id']
        prefix = self._prefixes[cid]
        position = self._positions[cid]
        self.recovery.assert_immutable(cid)
        if position == len(prefix):
            _require(self._continued[cid], 'new Target execution before the first resumed revision')
            return super().execute(case, policy_id, payload, phase, ordinal)
        record = prefix[position]
        old = record['episode']
        eid = f'{cid}/{policy_id}/{ordinal:03d}'
        _require(eid == old['episode_id'] and phase == old['phase']
                 and ordinal == position and payload.id == old['payload_id']
                 and payload.sha256 == old['payload_sha256']
                 and payload.text == record['raw']['payload'], 'requested execution does not match the frozen prefix')
        provenance = dict(record_origin=ORIGIN, source_run=str(self.recovery.source_run),
                          source_episode_id=eid)
        self.store.append('events', dict(event='TARGET_STARTED', episode_id=eid,
            payload_sha256=payload.sha256, **provenance))
        destination = self.store.path / old['raw_path']
        _require(not destination.exists(), 'destination raw already exists')
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_key = cid + '/' + record['source_raw_path']
        data = self.recovery._raw_bytes[source_key]
        _require(_hash(data) == record['source_raw_sha256'], 'cached source raw hash changed')
        with destination.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        episode = Episode(**deepcopy(old))
        result = record['raw']['result']
        self.packets[eid] = tuple(deepcopy(record['raw']['query_packets']))
        self.attempts[eid] = dict(analysis=record['analysis'], injection=payload.text,
            security=result['gold'], utility=result['utility'],
            final_output=(result['black_box_final'] if result['black_box_final'] is not None else result['final'])[:4000])
        self.episodes.append(episode)
        self.store.append('episodes', episode)
        self.store.append('events', dict(event='TARGET_COMPLETED', episode_id=eid,
                                         status=episode.status, **provenance))
        self.store.append('events', dict(event='PREFIX_EXECUTION_RECOVERED', episode_id=eid,
            source_raw_sha256=record['source_raw_sha256'], **provenance))
        self._positions[cid] += 1
        self._recovered_count += 1
        return episode

    def metrics(self):
        metrics = super().metrics()
        metrics['recovery'] = deepcopy(self.recovery.metadata)
        metrics['recovered_prefix_target_executions'] = self._recovered_count
        metrics['new_target_executions'] = len(self.episodes) - self._recovered_count
        metrics['physical_total_cost'].update(
            recovered_prefix_target_executions=self._recovered_count,
            new_target_executions=len(self.episodes)-self._recovered_count)
        return metrics
