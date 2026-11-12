"""Offline public prompt reconstruction and fresh Target provenance for schema 2.

No historical source outcomes are used. Paid-call provenance and cohort claims
are separate launch checks; a prompt replay alone is not evidence of efficacy.
"""
import json
from pathlib import Path

from scripts.export_fresh_initializations import audit_record
from scripts import verify_a2_v2_test30_20261030 as audit
from src.attacker.initialization import InitializationBundle, sha256_bytes
from src.attacker.original import OriginalPIMinerAdapter
from src.attacker.piminer import GenerationError


def verify_sources(root, bundle_path, cases, target_model):
    root = Path(root).resolve()
    bundle = InitializationBundle(root, bundle_path)
    audit.require(bundle.schema_version == 2, 'fresh_source_requires_schema2')
    audit.require(list(bundle.case_ids) == [c['case_id'] for c in cases], 'fresh_cohort_changed')
    rows = []
    for case in cases:
        entry = bundle.for_case(case)
        path = root / entry['source_metadata']['record_path']
        record = json.loads(path.read_text())
        calls = record['generation']['calls']
        audit.require([c['stage'] for c in calls] == ['router', 'attacker_p0']
                      and all(c['status'] == 'completed' for c in calls), 'fresh_source_calls')
        pending = iter(calls)
        used = []

        def replay(prompt, stage):
            saved = next(pending)
            audit.require(saved['prompt'] == prompt and saved['stage'] == stage,
                          'fresh_public_prompt_mismatch')
            used.append(stage)
            return saved['output']

        replay.offline_replay = True
        attacker = OriginalPIMinerAdapter(root, 'offline-replay', target_model, decide=replay)
        try:
            result = audit_record(attacker, case, record)
        except GenerationError as exc:
            raise ValueError('Fresh offline replay failed: ' + str(exc.__cause__)) from exc
        audit.require(len(used) == len(calls), 'fresh_source_unused_calls')
        rows.append(dict(**result, payload_sha256=sha256_bytes(entry['payload'].encode()),
                         source_record_sha256=sha256_bytes(path.read_bytes())))
    return dict(status='passed', api_calls=0, assigned_cases=len(cases),
                runtime_manifest=str(bundle.path),
                runtime_manifest_sha256=sha256_bytes(bundle.path.read_bytes()), cases=rows)


def verify_initial_execution(run, entry):
    cid = entry['case_id']
    payload_sha256 = sha256_bytes(entry['payload'].encode())
    directory = Path(run) / cid
    events, torn = audit.rows(directory / 'events.jsonl')
    starts = [e for e in events if e['event'] == 'TARGET_STARTED']
    imported = [e for e in events if e['event'] == 'GENERATION_COMPLETED'
                and e['generation_id'] == cid + '/shared/P0']
    audit.require(not torn and len(imported) == 1 and 1 <= len(starts) <= 5,
                  'fresh_initial_missing_duplicate_or_budget')
    record = audit.read(audit.safe_raw(directory, imported[0]['raw_path']))
    audit.require(record['payload'] == entry['payload']
                  and record['payload_sha256'] == payload_sha256
                  and record['analysis'] == entry['analysis']
                  and record['source_metadata'] == entry['source_metadata']
                  and record['calls'] == [] and record['candidates'] == []
                  and record['initialization'] == 'fresh_shared_route_and_p0'
                  and record['reused_target_feedback'] is False, 'fresh_initial_import_changed')
    first = starts[0]
    audit.require(first['episode_id'] == cid + '/shared/000'
                  and first['payload_sha256'] == payload_sha256
                  and first.get('record_origin', 'new_execution') == 'new_execution',
                  'fresh_initial_target_reused')
    episodes, torn = audit.rows(directory / 'episodes.jsonl')
    initial = [e for e in episodes if e['episode_id'] == first['episode_id']]
    audit.require(not torn and len(initial) == 1, 'fresh_initial_target_incomplete')
    episode = initial[0]
    saved = audit.read(audit.safe_raw(directory, episode['raw_path']))
    audit.require(saved['payload'] == entry['payload'] and episode['payload_id'] == 'P0'
                  and episode['phase'] == 'initial', 'fresh_initial_target_bytes')
    return dict(status='passed', case_id=cid, target_starts=len(starts),
                initial_target_gold=episode['gold'], initial_target_status=episode['status'])
