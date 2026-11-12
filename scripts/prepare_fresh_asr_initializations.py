"""Prepare one shared original route/P0 per assigned case, with zero Target calls.

Default: validate a hash-bound protocol offline. Future paid generation requires
--execute AND protocol paid_execution_authorized/candidate_frozen both true.
Run this script from the frozen source snapshot named by the protocol. See
--help and tests/test_prepare_fresh_asr_initializations.py for the input schema.
There is no resume/retry mode; any failed cohort remains incomplete for review.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.export_fresh_initializations import durable_json, export_records, generate_one
from src.attacker.accounting_proxy import AccountingProxy
from src.attacker.call_logging import CallsStopped, check_stop, now
from src.attacker.initialization import local_path, public_identity, read_json, sha256_bytes
from src.attacker.original import OriginalPIMinerAdapter
from src.attacker.piminer import GenerationError
from src.attacker.transport import RoleDecision


SEMANTICS = dict(generation_attempts_per_case=1, cli_max_retries=2,
                 target_executions=0, arms=2, repeats=3,
                 share_per_arm=0.5, multiply_cost_by_repeats=False)


def source_files(root):
    return {str(p.relative_to(root)) for directory in ('src', 'scripts')
            for p in (root / directory).rglob('*')
            if p.is_file() and p.suffix in ('.py', '.txt')} | {'main.py'}


def strategy_files(root):
    # Pin the complete discovery surface, including the cold-start template.
    directory = root / 'strategy_library'
    return {str(p.relative_to(root)) for p in directory.glob('*.md') if p.is_file()}


def checked_files(root, values):
    if not isinstance(values, dict) or not values:
        raise ValueError('Missing pinned input files')
    for name, digest in values.items():
        if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
                or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise ValueError('Invalid project-relative input hash')
        if sha256_bytes(local_path(root, name).read_bytes()) != digest:
            raise ValueError('Pinned input changed: ' + name)


def validate_protocol(root, path, expected_sha256, execute=False):
    """Only public case identities are projected; no historical outcomes read."""
    root = Path(root).resolve()
    path = local_path(root, path)
    raw = path.read_bytes()
    if sha256_bytes(raw) != expected_sha256:
        raise ValueError('Protocol SHA-256 differs')
    value = read_json(raw.decode())
    if value.get('schema_version') != 1 or value.get('kind') != 'fresh_shared_initialization':
        raise ValueError('Invalid fresh initialization protocol')
    if value.get('semantics') != SEMANTICS:
        raise ValueError('Protocol must preserve fixed cohort, attempts, sharing and accounting')
    if value.get('source_snapshot') != str(root):
        raise ValueError('Execute from the named frozen source snapshot')
    inputs = value['input_sha256']
    checked_files(root, inputs)
    required = ('cases_path', 'source_manifest_path', 'reservation_path',
                'preparation_audit_path', 'candidate_freeze_path')
    if any(value.get(key) not in inputs for key in required):
        raise ValueError('Protocol provenance inputs must all be hash-bound')
    manifest = read_json(local_path(root, value['source_manifest_path']).read_text())
    if manifest.get('snapshot') != str(root) or set(manifest['files']) != source_files(root):
        raise ValueError('Frozen source inventory differs')
    checked_files(root, manifest['files'])
    if set(value['strategy_sha256']) != strategy_files(root):
        raise ValueError('Strategy discovery inventory differs')
    checked_files(root, value['strategy_sha256'])
    cases = [read_json(line) for line in local_path(root, value['cases_path']).read_text().splitlines()
             if line.strip()]
    ids = [case.get('case_id') for case in cases]
    if (not ids or ids != sorted(ids) or len(set(ids)) != len(ids)
            or any(not isinstance(cid, str) or re.fullmatch('c[0-9]{3}', cid) is None for cid in ids)
            or value.get('case_ids') != ids or value.get('assigned_cases') != len(ids)):
        raise ValueError('Whole assigned cohort/order must match the pinned dataset')
    if len({(c['suite_name'], c['user_task_id']) for c in cases}) != len(cases):
        raise ValueError('The assigned cohort must have one case per base task')
    # Discard every other field before constructing even the adapter input.
    cases = [dict(case_id=c['case_id'], **public_identity(c)) for c in cases]
    for key in ('attacker_model', 'target_model', 'attacker_effort'):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError('Missing explicit model/effort')
    if type(value.get('timeout_seconds')) is not int or value['timeout_seconds'] <= 0:
        raise ValueError('Missing positive invocation timeout')
    if execute:
        if value.get('paid_execution_authorized') is not True or value.get('candidate_frozen') is not True:
            raise ValueError('Paid initialization requires explicit authorization and candidate freeze')
        freeze = read_json(local_path(root, value['candidate_freeze_path']).read_text())
        if freeze.get('candidate_frozen') is not True or freeze.get('candidate_id') != value.get('candidate_id') or not value.get('candidate_id'):
            raise ValueError('Candidate freeze record does not match protocol')
    return value, cases


@contextmanager
def environment(values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class DurableDecision:
    """Persist each stage before dispatch and after return, even on failure."""
    def __init__(self, delegate, role, record_path, record, guard):
        self.delegate, self.role = delegate, role
        self.record_path, self.record, self.guard = record_path, record, guard

    @property
    def calls(self):
        return self.delegate.calls

    def __call__(self, prompt, stage):
        self.guard()
        check_stop()
        item = dict(stage=stage, prompt=prompt, system_prompt=self.role,
                    status='running', provider=None)
        self.record['generation']['calls'].append(item)
        durable_json(self.record_path, self.record)
        before = len(self.calls)
        try:
            output = self.delegate(prompt, stage)
            item.update(status='completed', output=output)
            check_stop()
            return output
        except BaseException as exc:
            item.update(status='error', error_type=type(exc).__name__)
            raise
        finally:
            item['provider'] = deepcopy(self.calls[-1]) if len(self.calls) > before else None
            durable_json(self.record_path, self.record)


def empty_record(case):
    return dict(schema_version=1, kind='fresh_public_generation', case_id=case['case_id'],
                identity=public_identity(case), generated_at=now(), target_executions=0,
                history=[], generation=dict(status='not_started', calls=[]))


def verify_accounting(path, records):
    """Require durable CLI/HTTP evidence; missing usage/cost is never zeroed."""
    latest = {}
    for line in path.read_text().splitlines():
        entry = read_json(line)
        latest[entry['call_id']] = entry
    if any(r.get('kind') not in ('attacker_cli', 'attacker_http') for r in latest.values()):
        raise ValueError('Unexpected call kind in initialization journal')
    expected = {c['provider']['call_id'] for r in records for c in r['generation']['calls']}
    cli = {cid: r for cid, r in latest.items() if r['kind'] == 'attacker_cli'}
    if set(cli) != expected or len(expected) != 2 * len(records):
        raise ValueError('Generation stages and CLI journal differ')
    if any(r['event'] != 'finish' or r['status'] != 'completed' or not r['dispatched'] for r in cli.values()):
        raise ValueError('Unfinished CLI accounting')
    http = [r for r in latest.values() if r['kind'] == 'attacker_http']
    if (any(r.get('parent_call_id') not in cli or r['event'] != 'finish' for r in http)
            or any(not any(r.get('parent_call_id') == cid and r.get('dispatched') for r in http) for cid in cli)):
        raise ValueError('Missing/unfinished per-request accounting')
    return dict(status='passed', logical_cli_invocations=len(cli),
                actual_http_attempts=sum(bool(r.get('dispatched')) for r in http),
                target_executions=0, cost_usd=None,
                note='Summarize actual HTTP journal separately; unknown costs stay unknown')


def prepare(root, protocol_path, protocol_sha256, output, execute=False,
            decision_factory=RoleDecision, proxy_factory=AccountingProxy):
    root = Path(root).resolve()
    protocol, cases = validate_protocol(root, protocol_path, protocol_sha256, execute)
    output = local_path(root, output)
    if output.exists():
        raise FileExistsError('Initialization output must be new; no silent resume/retry')
    summary = dict(schema_version=1, status='offline_validated', api_calls=0,
                   assigned_cases=len(cases), case_ids=[c['case_id'] for c in cases],
                   target_executions=0, target_preflight_calls=0, attacker_preflight_calls=0,
                   protocol_sha256=protocol_sha256, semantics=SEMANTICS,
                   cost_phase='shared_initialization', cost_journal='calls.jsonl',
                   bundle=None)
    if not execute:
        return summary
    upstream = os.environ.get('ANTHROPIC_BASE_URL', '')
    if not upstream or not (os.environ.get('ANTHROPIC_AUTH_TOKEN') or os.environ.get('ANTHROPIC_API_KEY')):
        raise ValueError('Set explicit Attacker endpoint and credentials; no Target credentials required')
    output.mkdir(parents=True, exist_ok=False)
    records = [empty_record(case) for case in cases]
    paths = [output / c['case_id'] / 'raw/generation.json' for c in cases]
    for path, record in zip(paths, records):
        durable_json(path, record)
    durable_json(output / 'protocol.json', protocol)
    (output / 'calls.jsonl').touch(exist_ok=False)
    summary.update(status='running', api_calls=None, started_at=now())
    durable_json(output / 'manifest.json', summary)

    def guard():
        validate_protocol(root, protocol_path, protocol_sha256, execute=True)

    proxy = None
    try:
        with environment(dict(PIMINER_CALL_LOG=str(output / 'calls.jsonl'),
                              PIMINER_STOP_FILE=str(output / 'STOP'),
                              PIMINER_ACCOUNTING_PROXY='1', PIMINER_RUN_ID=str(output))):
            guard()
            proxy = proxy_factory(upstream, timeout=protocol['timeout_seconds'])
            with environment(dict(ANTHROPIC_BASE_URL=proxy.start())):
                for index, (case, path) in enumerate(zip(cases, paths)):
                    if (output / 'STOP').exists():
                        break
                    guard()
                    record = records[index]
                    record['generation']['status'] = 'running'
                    durable_json(path, record)
                    try:
                        role = (root / 'src/vendor/attacker_role.txt').read_text()
                        def decision(role):
                            delegate = decision_factory(protocol['attacker_model'], protocol['attacker_effort'],
                                                        protocol['timeout_seconds'], role)
                            return DurableDecision(delegate, role, path, record, guard)
                        attacker = OriginalPIMinerAdapter(root, protocol['attacker_model'], protocol['target_model'],
                                                         decide=decision(role), route_decide=decision(''))
                        # Neither Target nor any preflight is constructed/called.
                        with environment(dict(PIMINER_RUN_ID=str(output / case['case_id']))):
                            records[index] = generate_one(attacker, case)
                    except GenerationError as exc:
                        record['generation'] = deepcopy(exc.record)
                    except BaseException as exc:
                        record['generation'].update(status='interrupted', error_type=type(exc).__name__)
                        raise
                    finally:
                        durable_json(path, records[index])
                guard()
                if (output / 'STOP').exists() or any(r['generation']['status'] != 'ready' for r in records):
                    summary['status'] = 'incomplete'
                else:
                    summary['accounting'] = verify_accounting(output / 'calls.jsonl', records)
                    bundle = export_records(root, protocol['cases_path'], output / 'bundle', records)
                    from scripts.audit_fresh_initializations import verify_sources
                    replay = verify_sources(root, bundle, cases, protocol['target_model'])
                    durable_json(output / 'public_prompt_replay.json', replay)
                    guard()
                    summary.update(status='ready', bundle=str(bundle.relative_to(root)),
                                   bundle_sha256=sha256_bytes(bundle.read_bytes()))
    except BaseException as exc:
        summary.update(status='incomplete', error_type=type(exc).__name__)
        raise
    finally:
        if proxy is not None:
            proxy.close()
        summary.update(finished_at=now(), case_statuses={r['case_id']: r['generation']['status'] for r in records},
                       stop_marker=(output / 'STOP').exists(),
                       journal_sha256=sha256_bytes((output / 'calls.jsonl').read_bytes()))
        # A failed replay/export cannot leave a loadable complete bundle.
        if summary['status'] != 'ready' and (output / 'bundle/initializations.json').exists():
            (output / 'bundle/initializations.json').rename(output / 'bundle/initializations.rejected.json')
        durable_json(output / 'manifest.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', required=True, help='Project-relative, frozen protocol JSON')
    parser.add_argument('--protocol-sha256', required=True, help='Approved exact protocol bytes')
    parser.add_argument('--output', required=True, help='New project-relative run directory')
    parser.add_argument('--execute', action='store_true', help='Permit generation only with frozen paid authorization')
    args = parser.parse_args()
    result = prepare(ROOT, args.protocol, args.protocol_sha256, args.output, args.execute)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('ready', 'offline_validated') else 2


if __name__ == '__main__':
    raise SystemExit(main())
