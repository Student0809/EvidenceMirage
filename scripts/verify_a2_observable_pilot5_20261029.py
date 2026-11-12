"""Offline A2 pilot verification, including exact prompts and branch histories.

No model execution or benchmark resubmission occurs. The default accepts an
incomplete snapshot as `incomplete`; --require-complete requires all five cases.
"""
import argparse
import ast
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.verify_observable_pilot5_20261029 import (
    EXPECTED_ARMS, EXPECTED_CASES, VerificationError, read, require,
    verify_case as verify_native_case, verify_episode)
from src.engine.config import check_frozen, digest
from src.engine.storage import write_json
from src.schema import payload_hash
from src.vendor import a2_converter, router
from src.vendor.prompts import build_user_message_blocks

PUBLIC_FIELDS = ('suite_name', 'user_task_id', 'injection_task_id', 'user_task_prompt',
                 'injection_task_goal', 'context_with_placeholder', 'injection_candidates')


def text_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def local(path):
    result = (ROOT / path).resolve()
    require(result.is_relative_to(ROOT), 'resource_outside_project')
    return result


def raw_file(directory, relative):
    path = (directory / relative).resolve()
    require(path.is_relative_to(directory.resolve()), 'raw_path_outside_case')
    return path


def snapshot_lines(path, partial=False):
    if not path.exists() and partial:
        return [], False
    text = path.read_text()
    lines, rows, torn = text.splitlines(), [], False
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            if partial and i == len(lines) - 1 and not text.endswith('\n'):
                torn = True
            else:
                raise VerificationError('invalid_jsonl_' + path.name)
    return rows, torn


def upstream_functions():
    """Load the pinned MiniPIM reference functions, not the runtime A2 adapter."""
    namespace = {}
    for relative, names in (
        ('strategy_library_a2/upstream/raw_ablation.py', {'select_cases', 'render_variant'}),
        ('strategy_library_a2/upstream/library_token_audit.py', {'partition'}),
    ):
        tree = ast.parse(local(relative).read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        require({node.name for node in nodes} == names, 'missing_upstream_reference')
        exec(compile(ast.Module(body=nodes, type_ignores=[]), relative, 'exec'), namespace)
    return namespace


class A2Reconstruction:
    def __init__(self, config):
        self.config = config
        options = config['attacker']
        require(options['mode'] == 'a2', 'wrong_attacker_mode')
        self.library_path = local(options['a2_library'])
        self.retrieval_path = local(options['a2_retrieval'])
        self.library, self.retrieval = read(self.library_path), read(self.retrieval_path)
        require(self.retrieval['library_sha256'] == digest(self.library_path), 'retrieval_library_hash')
        self.reference = upstream_functions()
        self.strategies, self.cases = {}, deepcopy(self.library['cases'])
        by_id = {case['case_id']: case for case in self.cases}
        self.originals = {}
        for sid, strategy in self.library['strategies'].items():
            path = local(strategy['source'])
            text = path.read_bytes().decode('utf-8')
            require(digest(path) == strategy['source_sha256'], 'source_hash_changed')
            document = a2_converter.convert_document(text, path.name)
            segments, examples = self.reference['partition'](document, a2_converter)
            method = ''.join(value for kind, value in segments if kind == 'method')
            require(method == strategy['method'], 'source_method_mismatch')
            self.strategies[sid] = dict(id=sid, method=method)
            self.originals[sid] = path.read_text()
            for ordinal, (_, raw) in enumerate(examples, 1):
                cid = f'{sid}::example-{ordinal:03d}'
                require(by_id[cid]['raw'] == raw, 'source_case_mismatch')
                by_id[cid]['raw'] = raw
        self.role = local('src/vendor/attacker_role.txt').read_text()
        self.router_strategies = router._discover_strategies('agentdojo')

    def compose(self, case, ids):
        task = self.retrieval['tasks'][case['case_id']]
        public = {key: case[key] for key in ('case_id', *PUBLIC_FIELDS)}
        public_digest = text_hash(json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
        require(public_digest == task['public_sha256'], 'public_case_hash')
        selected, selections = {}, {}
        for sid in ids:
            chosen, log = self.reference['select_cases'](self.cases, strategy_id=sid,
                scenario=case['suite_name'], task_type=task['task_type'],
                current_group=['agentdojo', case['suite_name'], case['user_task_id']],
                scores=task['scores'], k=2)
            selected[sid] = chosen
            selections[sid] = dict(case_ids=[c['case_id'] for c in chosen], log=log)
            require(selections[sid] == task['selections'][sid], 'upstream_selection_mismatch')
            require(all(c['group'] != ['agentdojo', case['suite_name'], case['user_task_id']]
                        for c in chosen), 'current_task_case_leak')
        texts = self.reference['render_variant']({sid: self.strategies[sid] for sid in ids}, selected, 'A2')
        document = '\n\n'.join(f'===== Candidate strategy {i}/{len(ids)}: {sid} =====\n\n{texts[sid]}'
                               for i, sid in enumerate(ids, 1))
        original = '\n\n'.join(f'===== Candidate strategy {i}/{len(ids)}: {sid} =====\n\n{self.originals[sid]}'
                               for i, sid in enumerate(ids, 1))
        paths = [self.library['strategies'][sid]['source'] for sid in ids]
        metadata = dict(protocol='minipim-a2-v1', library_sha256=digest(self.library_path),
            retrieval_sha256=digest(self.retrieval_path), public_sha256=public_digest,
            task_type=task['task_type'], selections=selections, strategy_document_sha256=text_hash(document),
            original_document_chars=len(original), a2_document_chars=len(document))
        return document, dict(strategy_ids=ids, strategy_paths=paths,
            strategy_sha256={path: digest(local(path)) for path in paths}, a2=metadata)


def verify_generations(directory, case, manifest, reconstruction=None, partial=False):
    """Match persisted calls to reference prompts built from event-ordered history."""
    reference = reconstruction or A2Reconstruction(manifest['config'])
    events, torn_events = snapshot_lines(directory / 'events.jsonl', partial)
    episodes, torn_episodes = snapshot_lines(directory / 'episodes.jsonl', partial)
    decisions, torn_decisions = snapshot_lines(directory / 'decisions.jsonl', partial)
    by_id = {episode['episode_id']: episode for episode in episodes}
    require(len(by_id) == len(episodes), 'duplicate_episode')
    records, starts, targets, completed, rows = {}, {}, {}, [], []
    route_ids, route_meta, document = None, None, None
    public = {key: deepcopy(case[key]) for key in PUBLIC_FIELDS}
    router_prompt = router.ROUTER_ROLE_PROMPT + '\n\n' + router._format_routing_prompt(
        manifest['config']['target']['model'], public, reference.router_strategies)
    observed_revisions = Counter()

    def history_for(pid, ids):
        history = []
        for i, eid in enumerate(ids):
            episode = by_id[eid]
            require(episode['policy_id'] in {'shared', pid}, 'foreign_branch_generation_history')
            generation_pid = 'shared' if episode['payload_id'] == 'P0' else pid
            record = records[f"{case['case_id']}/{generation_pid}/{episode['payload_id']}"]
            saved = read(raw_file(directory, episode['raw_path']))
            result = saved['result']
            require(record['status'] == 'ready' and saved['payload'] == record['payload'], 'executed_payload_not_generated')
            final = result.get('black_box_final')
            final = result['final'] if final is None else final
            history.append(dict(iter=i, analysis=record.get('analysis', ''), injection=saved['payload'],
                security=result['gold'], utility=result['utility'], final_output=final[:4000]))
        return history

    for event in events:
        kind = event['event']
        if kind == 'GENERATION_STARTED':
            gid = event['generation_id']
            require(gid not in starts, 'duplicate_generation_start')
            cid, pid, revision = gid.split('/')
            require(cid == case['case_id'] and pid in EXPECTED_ARMS | {'shared'}, 'invalid_generation_identity')
            visible = [] if pid == 'shared' else [eid for eid in completed
                if by_id[eid]['policy_id'] in {'shared', pid}]
            if pid == 'shared':
                require(revision == 'P0' and not starts and not completed, 'invalid_shared_p0_generation')
            else:
                observed_revisions[pid] += 1
                require(revision == 'P' + str(observed_revisions[pid]), 'generation_revision_sequence')
                require(visible and visible[0] == case['case_id'] + '/shared/000', 'missing_shared_generation_history')
                require(by_id[visible[-1]]['gold'] is False and by_id[visible[-1]]['error'] is None,
                        'revise_without_valid_failure')
                revise = [d for d in decisions if d['policy_id'] == pid and d['action'] == 'Revise']
                require(len(revise) >= observed_revisions[pid] and
                        revise[observed_revisions[pid] - 1]['visible_history_ids'] == visible,
                        'generation_decision_history_mismatch')
            starts[gid] = (pid, visible)
        elif kind == 'GENERATION_COMPLETED':
            gid = event['generation_id']
            require(gid in starts and gid not in records, 'generation_completion_without_unique_start')
            pid, visible = starts[gid]
            record = read(raw_file(directory, event['raw_path']))
            require(record['case_id'] == case['case_id'] and record['policy_id'] == pid
                    and record['generation_id'] == gid and record['mode'] == 'a2', 'generation_record_identity')
            require(record['status'] == event['status'] and record['status'] in ('ready', 'GENERATION_ERROR'),
                    'generation_status_mismatch')
            calls = record['calls']
            expected_stages = ['router', 'attacker_p0'] if pid == 'shared' else ['attacker_revise']
            require([call['stage'] for call in calls] == expected_stages[:len(calls)]
                    and len(calls) <= len(expected_stages), 'unexpected_model_call_stages')
            if record['status'] == 'ready':
                require(len(calls) == len(expected_stages) and all(c['status'] == 'completed' for c in calls),
                        'ready_generation_missing_completed_calls')
            for call in calls:
                if call['stage'] == 'router':
                    require(call['prompt'] == router_prompt and call['system_prompt'] == '', 'router_prompt_mismatch')
                    if call['status'] == 'completed':
                        route_ids = router._parse_choices(call['output'], [s['id'] for s in reference.router_strategies])
                        if route_ids:
                            document, route_meta = reference.compose(case, route_ids)
                else:
                    require(document is not None and record.get('routing') == route_meta, 'a2_route_metadata_mismatch')
                    history = history_for(pid, visible)
                    expected = '\n'.join(build_user_message_blocks(document, public, history, threat_model='black_box'))
                    require(call['prompt'] == expected, 'attacker_prompt_or_branch_history_mismatch')
                    require(call['system_prompt'] == reference.role, 'attacker_system_role_mismatch')
                provider = call.get('provider')
                if provider is not None:
                    require(provider['stage'] == call['stage'] and provider['system_prompt'] == call['system_prompt']
                            and provider['model'] == manifest['config']['attacker']['model']
                            and provider['effort'] == manifest['config']['attacker']['effort']
                            and provider['status'] == call['status'], 'provider_record_mismatch')
            if record['status'] == 'ready':
                output = calls[-1]['output']
                analysis, injection = router._parse_attempt(output)
                injection = injection if injection.strip() else output
                require(record['analysis'] == analysis and record['payload'] == injection
                        and record['payload_sha256'] == payload_hash(injection), 'generated_payload_parse_mismatch')
                require(record['candidates'] == [output], 'generation_candidates_mismatch')
            records[gid] = record
            rows.append(dict(generation_id=gid, status=record['status'], visible_history_ids=visible,
                prompt_checks=len(calls), router_calls=sum(c['stage'] == 'router' for c in calls),
                payload_sha256=record.get('payload_sha256'), raw_sha256=digest(raw_file(directory, event['raw_path']))))
        elif kind == 'TARGET_STARTED':
            eid = event['episode_id']
            require(eid not in targets, 'duplicate_target_start')
            targets[eid] = event
        elif kind == 'TARGET_COMPLETED':
            eid = event['episode_id']
            require(eid in targets and eid not in completed, 'target_completion_without_unique_start')
            if eid not in by_id and partial:
                # A concurrent append can land after the episodes snapshot.
                break
            require(eid in by_id, 'completed_target_missing_episode')
            episode = by_id[eid]
            pid = 'shared' if episode['payload_id'] == 'P0' else episode['policy_id']
            gid = f"{case['case_id']}/{pid}/{episode['payload_id']}"
            require(gid in records and records[gid]['status'] == 'ready', 'target_missing_ready_generation')
            require(episode['payload_sha256'] == records[gid]['payload_sha256'] == targets[eid]['payload_sha256'],
                    'target_generation_payload_mismatch')
            require(event['status'] == episode['status'], 'target_completion_status_mismatch')
            saved = read(raw_file(directory, episode['raw_path']))
            require(saved['payload'] == records[gid]['payload'], 'target_raw_payload_mismatch')
            completed.append(eid)
        else:
            raise VerificationError('unexpected_event_kind')
    pending = sorted(set(starts) - set(records)) + sorted(set(targets) - set(completed))
    if not partial:
        require(not pending, 'unfinished_model_or_target_request')
        require(set(completed) == set(by_id), 'episode_missing_completion_event')
        for pid in EXPECTED_ARMS:
            require(observed_revisions[pid] == sum(d['policy_id'] == pid and d['action'] == 'Revise' for d in decisions),
                    'revise_without_generation')
    return dict(status='verified_snapshot' if partial else 'verified', generations=rows,
        prompt_checks=sum(row['prompt_checks'] for row in rows),
        router_calls=sum(row['router_calls'] for row in rows), completed_episode_ids=completed,
        pending_requests=pending, partial_jsonl=torn_events or torn_episodes or torn_decisions,
        strategy_ids=route_ids, a2=route_meta['a2'] if route_meta else None)


def verify_a2_case(directory, case, manifest, reconstruction=None):
    native = verify_native_case(directory, case, manifest)
    generation = verify_generations(directory, case, manifest, reconstruction)
    return dict(native, generation_verification=generation)


def verify_run(source, require_complete=False):
    source = Path(source).resolve()
    issues, rows, pending = [], [], []
    manifest = read(source / 'manifest.json')
    cases = check_frozen(manifest)
    require([case['case_id'] for case in cases] == EXPECTED_CASES, 'wrong_five_cases')
    require(manifest['cases'] == cases, 'parent_case_snapshot_mismatch')
    require(set(manifest['config']['policies']) == EXPECTED_ARMS, 'wrong_controller_arms')
    require(manifest['config']['post_failure_target_budget'] == 3, 'wrong_budget')
    reference = A2Reconstruction(manifest['config'])
    status = read(source / 'status.json')['status']
    terminal = status not in ('preflight', 'running')
    workers = read(source / 'worker_results.json') if (source / 'worker_results.json').exists() else []
    for case in cases:
        cid, directory = case['case_id'], source / case['case_id']
        try:
            case_status = read(directory / 'status.json')['status'] if (directory / 'status.json').exists() else 'not_started'
            worker = next((row for row in workers if row['case_id'] == cid), None)
            if case_status == 'completed':
                row = verify_a2_case(directory, case, manifest, reference)
                if worker is None and not terminal:
                    pending.append(dict(case_id=cid, status='awaiting_worker_result'))
                else:
                    require(worker and worker['status'] == 'completed', 'worker_not_completed')
                    require(worker['target_executions'] == row['target_executions'] and worker['outcomes'] == row['outcomes'],
                            'worker_result_mismatch')
                rows.append(row)
            else:
                snapshot = verify_generations(directory, case, manifest, reference, partial=True) if directory.exists() else None
                native_rows = []
                if snapshot:
                    episodes, _ = snapshot_lines(directory / 'episodes.jsonl', partial=True)
                    for episode in episodes:
                        if episode['episode_id'] in snapshot['completed_episode_ids']:
                            native_rows.append(verify_episode(directory, case, episode, manifest['config']))
                pending.append(dict(case_id=cid, status=case_status, generation_verification=snapshot, episodes=native_rows))
        except Exception as exc:
            issues.append(dict(case_id=cid, code=str(exc) if isinstance(exc, VerificationError) else type(exc).__name__))
    if terminal:
        if status != 'completed':
            issues.append(dict(code='run_not_completed', run_status=status))
        if len(workers) != 5 or {row['case_id'] for row in workers} != set(EXPECTED_CASES):
            issues.append(dict(code='missing_worker_results'))
    if pending and (require_complete or terminal):
        issues.append(dict(code='incomplete_cases', count=len(pending)))
    all_rows = rows + pending
    episodes = [episode for row in all_rows for episode in row.get('episodes', [])]
    target_count = len(episodes)
    require(target_count <= 35, 'total_budget_exceeded')
    generation_rows = [row['generation_verification'] for row in all_rows if row.get('generation_verification')]
    passed = not issues and not pending and len(rows) == 5 and status == 'completed'
    return dict(status='passed' if passed else 'failed' if issues else 'incomplete', api_calls=0,
        source=str(source), run_status=status, verified_cases=len(rows), expected_cases=5,
        verified_target_executions=target_count,
        native_scores_recomputed=sum(e['status'] == 'native_scores_recomputed' for e in episodes),
        target_errors_preserved=sum(e['status'] == 'target_error_preserved' for e in episodes),
        decisions_replayed=sum(row['decision_checks'] for row in rows),
        generation_prompt_checks=sum(row['prompt_checks'] for row in generation_rows),
        router_calls_checked=sum(row['router_calls'] for row in generation_rows),
        manifest_sha256=digest(source / 'manifest.json'),
        scope='Official native scores, saved-view Trigger, policy/budget replay; source-reconstructed A2 selection/documents, exact Router/Attacker prompts, branch-local history and generated/executed payload identity.',
        limitations=['No new model execution or resampling.',
            'Saved legal receiver views are checked for consistency, not independently recaptured from traces.',
            'BGE similarities are frozen inputs, not recomputed during runtime verification.',
            'Partial snapshots are never a completed integrity pass; verified records may precede current progress.',
            'Integrity checks do not establish efficacy or held-out generalization.'],
        issues=issues, pending_cases=pending, cases=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=str(ROOT / 'runs/a2_observable_pilot5_20261029'))
    parser.add_argument('--output', default=str(ROOT / 'reports/a2_observable_pilot5_verification_20261029.json'))
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args()
    report = verify_run(args.source, args.require_complete)
    output = Path(args.output).resolve()
    require(not output.is_relative_to(Path(args.source).resolve()), 'verification_output_inside_run')
    write_json(output, report)
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ('cases', 'pending_cases', 'limitations')}, ensure_ascii=False))
    return 1 if report['status'] == 'failed' else 0


if __name__ == '__main__':
    sys.exit(main())
