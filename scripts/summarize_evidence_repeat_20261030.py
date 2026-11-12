"""Read-only Evidence repeat summary; never imports an API client or prints payload text.

python scripts/summarize_evidence_repeat_20261030.py --run RUN \
  --reference-run runs/observable_pilot5_20261029 --outputprefix reports/evidence_repeat
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.analyze_piminer_asr3_20261029 import (
    EXPECTED_IDS, analyze_arm, cost_summary, decision_spent, first_episode, load_case,
    ordinal, prefix_generations, preflight_summary, read_json, safe_raw, sum_cost_reports,
    summarize,
)

from src.engine.config import data_fingerprints
from src.engine.budget import Budget
from src.engine.history import History
from src.policies.stage_aware import StageAware
from src.schema import Episode

POLICY = 'stage_aware'
LABELS = {'repeat': '本次 Evidence 全新运行', 'reference': '旧 Evidence 前三次前缀'}
CONFIG_KEYS = ('backend', 'target', 'attacker', 'observation', 'require_observable_trigger', 'seed')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def equality(left, right):
    """Report exact snapshot equality without reproducing potentially sensitive contents."""
    known = left is not None and right is not None
    return dict(equal=(left == right) if known else None,
                repeat_sha256=digest(left) if left is not None else None,
                reference_sha256=digest(right) if right is not None else None)


def case_map(manifest):
    return {c['case_id']: c for c in manifest.get('cases', [])
            if isinstance(c, dict) and isinstance(c.get('case_id'), str)}


def comparability(repeat, reference):
    rc, oc = repeat.get('config', {}), reference.get('config', {})
    checks = {key: equality(rc.get(key), oc.get(key)) for key in CONFIG_KEYS}
    checks['stage_aware'] = equality(rc.get('policies', {}).get(POLICY),
                                    oc.get('policies', {}).get(POLICY))
    checks['environment'] = equality(repeat.get('environment'), reference.get('environment'))
    rh = data_fingerprints(repeat.get('inputs_sha256', {}))
    oh = data_fingerprints(reference.get('inputs_sha256', {}))
    inputs = {key: dict(equal=(rh[key] == oh[key]) if key in rh and key in oh else None,
                       repeat_sha256=rh.get(key), reference_sha256=oh.get(key))
              for key in sorted(set(rh) | set(oh))}
    rcs, ocs = case_map(repeat), case_map(reference)
    snapshots = {cid: equality(rcs.get(cid), ocs.get(cid)) for cid in EXPECTED_IDS}
    all_checks = list(checks.values()) + list(inputs.values()) + list(snapshots.values())
    return dict(configuration=checks, data_input_hashes=inputs, source_code_compared=False, case_snapshots=snapshots,
        all_recorded_checks_equal=all(c['equal'] is True for c in all_checks),
        mismatched_checks=[k for group in (checks, inputs, snapshots)
                           for k, v in group.items() if v['equal'] is False],
        unavailable_checks=[k for group in (checks, inputs, snapshots)
                            for k, v in group.items() if v['equal'] is None],
        repeat_post_failure_budget=rc.get('post_failure_target_budget'),
        reference_post_failure_budget=oc.get('post_failure_target_budget'),
        budgets_identical=rc.get('post_failure_target_budget') == oc.get('post_failure_target_budget'),
        initial_identity_required=False, causal_effect_estimated=False,
        interpretation='新运行与旧 Evidence 前三次前缀按 TotalASR@3 比较；旧组未整组重跑。'
                       '后续预算记录为旧 3、新 2，下面离线重放核对这两个预算下已有前两次继续决策的动作。')


def protocol_audit_summary(path, repeat, reference_run):
    """Expose a saved audit as historical context; never certify current code from it."""
    local_issues = []
    value = read_json(path, local_issues, required=False)
    if not isinstance(value, dict):
        return dict(status='unavailable', source=str(path), applicable=False)
    source = reference_run/'manifest.json'
    matches = (source.is_file() and value.get('source_manifest_sha256')
               == hashlib.sha256(source.read_bytes()).hexdigest())
    reconstruction, tests = value.get('historical_prompt_reconstruction', {}), value.get('tests', {})
    return dict(status=value.get('status'), source=str(path), applicable=False,
                reference_manifest_matches=matches, source_code_compared=False,
                original_mode_tests=dict(status=tests.get('status'), tests=tests.get('tests'), api_calls=tests.get('api_calls')),
                historical_prompt_reconstruction={key: reconstruction.get(key) for key in
                    ('ready_generations_checked', 'prompt_calls_compared', 'all_prompts_and_roles_byte_identical',
                     'all_payload_hashes_identical', 'failed_generations_excluded', 'api_calls')},
                scope_note='旧审计仅作为历史记录；已移除源码哈希检查，不能据此认证当前代码语义。')


def event_cost(data, cid, *, prefix_only):
    """Charge unique completed events, keeping started requests with no cost record visible."""
    def selected_target(eid):
        return (isinstance(eid, str) and eid.startswith(cid+'/') and
                (not prefix_only or eid == f'{cid}/shared/000' or ordinal(eid, cid, POLICY) in (1, 2)))
    if prefix_only:
        selected_gids = set()
        for pid in ('shared', POLICY):
            _, gids, _ = prefix_generations(data, cid, pid)
            selected_gids.update(gids)
    else:
        selected_gids = {e.get('generation_id') for e in data['events']
                         if isinstance(e.get('generation_id'), str)
                         and e['generation_id'].startswith(cid+'/')}
    starts, completed, gen_starts, gen_completed = set(), set(), set(), set()
    generations = []
    for event in data['events']:
        kind, eid, gid = event.get('event'), event.get('episode_id'), event.get('generation_id')
        if selected_target(eid) and kind == 'TARGET_STARTED':
            starts.add(eid)
        if selected_target(eid) and kind == 'TARGET_COMPLETED':
            if eid in completed:
                data['issues'].append(dict(code='duplicate_target_completed', episode_id=eid))
            completed.add(eid)
        if gid in selected_gids and kind == 'GENERATION_STARTED':
            gen_starts.add(gid)
        if gid in selected_gids and kind == 'GENERATION_COMPLETED':
            if gid in gen_completed:
                data['issues'].append(dict(code='duplicate_generation_completed', generation_id=gid))
                continue
            gen_completed.add(gid)
            record = safe_raw(data['folder'], event.get('raw_path'), data['issues'])
            if isinstance(record, dict):
                generations.append(record)
    by_id = {e.get('episode_id'): e for e in data['episodes'] if e.get('case_id') == cid}
    target_rows = [by_id[eid] for eid in sorted(completed) if eid in by_id]
    missing_rows = sorted(completed-set(by_id))
    for eid in missing_rows:
        data['issues'].append(dict(code='completed_target_missing_episode', episode_id=eid))
    unresolved_gens = sorted(gen_starts-gen_completed)
    result = cost_summary(target_rows, generations, started_ids=starts,
                          generation_ids=gen_starts, unresolved_generations=unresolved_gens)
    result.update(target_completed_events=len(completed), generation_completed_events=len(gen_completed),
                  unresolved_target_ids=sorted(starts-completed), completed_target_missing_episode_ids=missing_rows)
    if (not (data['folder']/'events.jsonl').is_file()
            or not (data['folder']/'episodes.jsonl').is_file()
            or missing_rows or starts-completed or gen_starts-gen_completed
            or len(generations) != len(gen_completed) or completed-starts or gen_completed-gen_starts):
        result['total_tokens_complete'] = False
    return result


def sum_event_costs(reports):
    result = sum_cost_reports(reports)
    for key in ('target_completed_events', 'generation_completed_events'):
        result[key] = sum(r[key] for r in reports)
    for key in ('unresolved_target_ids', 'completed_target_missing_episode_ids'):
        result[key] = [item for r in reports for item in r[key]]
    return result


def replay_budget_prefix(data, cid):
    """Recompute saved continuation decisions under the old limit and B=2, with no API calls."""
    config = data['manifest'].get('config', {})
    old_limit = config.get('post_failure_target_budget')
    by_id = {e.get('episode_id'): e for e in data['episodes'] if e.get('case_id') == cid}
    records = [d for d in data['decisions'] if d.get('case_id') == cid and d.get('policy_id') == POLICY
               and d.get('action') in ('Retry', 'Revise') and decision_spent(d) in (0, 1)]
    rows = []
    for recorded in records:
        spent = decision_spent(recorded)
        row = dict(decision_id=recorded.get('decision_id'), spent_target_budget=spent,
                   recorded_action=recorded.get('action'), recorded_reason=recorded.get('reason'),
                   old_budget=old_limit, repeat_budget=2, replayed=False)
        try:
            if type(old_limit) is not int or old_limit < 2:
                raise ValueError('invalid reference budget')
            if POLICY not in config.get('policies', {}):
                raise ValueError('missing policy configuration')
            expected_ids = [f'{cid}/shared/000'] + [f'{cid}/{POLICY}/{i:03d}' for i in range(1, spent+1)]
            if recorded.get('visible_history_ids') != expected_ids:
                raise ValueError('unexpected visible history')
            episodes = [Episode(**by_id[eid]) for eid in expected_ids]
            policy = StageAware(**(config['policies'][POLICY] or {}))
            old = policy.decide(History.from_episodes(episodes, Budget(old_limit, spent)))
            new = policy.decide(History.from_episodes(episodes, Budget(2, spent)))
            row.update(replayed=True, old_budget_action=old.action.value, repeat_budget_action=new.action.value,
                       old_budget_reason=old.reason, repeat_budget_reason=new.reason,
                       recorded_action_and_reason_match=(old.action.value == recorded.get('action')
                                                         and old.reason == recorded.get('reason')),
                       budget_actions_and_reasons_equal=(old.action == new.action and old.reason == new.reason))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            row['error_type'] = type(exc).__name__
        rows.append(row)
    verified = all(r.get('replayed') and r.get('recorded_action_and_reason_match')
                   and r.get('budget_actions_and_reasons_equal') for r in rows)
    return dict(case_id=cid, recorded_continue_decisions=len(records), decisions_replayed=sum(r['replayed'] for r in rows),
                all_recorded_decisions_match=verified if rows else None,
                source_code_compared=False, decisions=rows)


def summarize_budget_replay(cases):
    checks = [case['reference']['budget_prefix_replay'] for case in cases]
    decisions = [row for check in checks for row in check['decisions']]
    success = (bool(decisions) and all(row.get('replayed') and row.get('recorded_action_and_reason_match')
               and row.get('budget_actions_and_reasons_equal') for row in decisions))
    return dict(status='verified' if success else 'requires_review' if decisions else 'unavailable',
                recorded_continue_decisions=len(decisions), decisions_replayed=sum(row['replayed'] for row in decisions),
                all_recorded_actions_and_reasons_match=success if decisions else None,
                source_code_compared=False,
                scope='使用当前策略实现，仅将旧 StageAware 已记录的 spent=0/1 继续决策及相同可见历史分别用旧预算和 B=2 重放；'
                      '不重新生成候选或调用 Target，不推断新候选下的反事实结果。',
                api_calls=0)


def success_details(arm):
    success = next((s for s in arm['timeline'] if s['gold'] is True), None)
    return dict(gold_success=arm['gold_success'], first_success_attempt=success['overall_attempt'] if success else None,
                success_action=success['action'] if success else None,
                success_after_revision=(any(s['action'] == 'Revise' for s in arm['timeline']
                                            if s['overall_attempt'] <= success['overall_attempt']) if success else None))


def trigger_diagnostic(rows):
    failures = [step for row in rows for step in row['timeline'] if step['gold'] is False]
    hit = [step for step in failures if step['trigger'] == 'T']
    eligible = [node for row in rows for node in row['gold_false_trigger_true_nodes'] if node['within_budget_continuation_available']]
    actions = Counter(node['next_action'] for node in eligible if node['next_action'] is not None)
    return dict(valid_failed_executions=len(failures), gold_false_trigger_true_executions=len(hit),
                frequency_among_valid_failures=len(hit)/len(failures) if failures else None,
                eligible_nodes_with_budget=len(eligible), eligible_next_action_counts=dict(actions),
                eligible_retry_count=actions['Retry'], eligible_revise_count=actions['Revise'],
                eligible_nodes_without_recorded_action=sum(n['next_action'] is None for n in eligible),
                denominator_note='仅前三次内 Gold=False 的有效 Target 结果；错误/缺失不作为失败。'
                                 '有预算节点限定为第 1 或第 2 次结果，旧运行第 3 次后的动作不计入。')


def inspect_case(data, cid, role):
    initial = first_episode(data, cid)
    arm, _, _ = analyze_arm(data, cid, POLICY, initial)
    arm['label'] = LABELS[role]
    arm['provenance'] = 'fresh_generation_and_initial_execution' if role == 'repeat' else 'old_run_truncated_prefix'
    # Emit only outcome metadata; never serialize payloads, final responses, or free-form evidence.
    arm['timeline'] = [{k: step[k] for k in ('overall_attempt', 'action', 'episode_id', 'gold', 'trigger',
                                           'payload_id', 'payload_sha256', 'status')} for step in arm['timeline']]
    shared_records, shared_ids, shared_unresolved = prefix_generations(data, cid, 'shared')
    if initial is None and any(g.get('status') == 'GENERATION_ERROR' for g in shared_records):
        arm['status'] = 'GENERATION_ERROR'
        arm['timeline'][0]['status'] = 'GENERATION_ERROR'
        arm['generation_error_types'] += [g.get('error_type') for g in shared_records if g.get('status') == 'GENERATION_ERROR']
    arm['success'] = success_details(arm)
    nodes = []
    for step in arm['timeline']:
        if step['gold'] is not False or step['trigger'] != 'T':
            continue
        available = step['overall_attempt'] < 3
        following = [d for d in data['decisions'] if available and d.get('case_id') == cid
                     and d.get('policy_id') == POLICY and decision_spent(d) == step['overall_attempt']-1
                     and (d.get('visible_history_ids') or [None])[-1] == step['episode_id']]
        decision = next((d for d in following if d.get('action') in ('Retry', 'Revise')), None)
        if decision is None:
            decision = next(iter(following), None)
        nodes.append(dict(episode_id=step['episode_id'], overall_attempt=step['overall_attempt'],
                          within_budget_continuation_available=available,
                          next_action=decision.get('action') if decision else None,
                          recorded_remaining_budget=decision.get('remaining_target_budget') if decision else None))
    arm['gold_false_trigger_true_nodes'] = nodes
    arm['cost_with_shared_initial'] = event_cost(data, cid, prefix_only=True)
    # Replace the helper's episode-based cost with completion-event accounting throughout.
    arm.pop('post_failure_cost', None)
    shared_eid = f'{cid}/shared/000'
    kinds = {e.get('event') for e in data['events'] if e.get('episode_id') == shared_eid}
    arm['initial_provenance'] = dict(
        fresh_generation_started=bool(shared_ids),
        fresh_generation_completed=bool(shared_records),
        unresolved_shared_generation_ids=shared_unresolved,
        shared_target_started='TARGET_STARTED' in kinds,
        shared_target_completed='TARGET_COMPLETED' in kinds,
        manifest_reused_initial=data['manifest'].get('reused_initial'),
        manifest_fresh_initial=data['manifest'].get('fresh_initial'),
        initial_payload_sha256=initial.get('payload_sha256') if initial else None)
    arm['case_run_status'] = data['status'].get('status')
    if role == 'reference':
        arm['budget_prefix_replay'] = replay_budget_prefix(data, cid)
    return arm


def analyze(run, reference_run, protocol_audit=None):
    run, reference_run = Path(run).resolve(), Path(reference_run).resolve()
    issues = []
    manifests = {role: read_json(path/'manifest.json', issues, default={})
                 for role, path in (('repeat', run), ('reference', reference_run))}
    manifests = {k: v if isinstance(v, dict) else {} for k, v in manifests.items()}
    for role, manifest in manifests.items():
        ids = [c.get('case_id') for c in manifest.get('cases', []) if isinstance(c, dict)]
        if len(ids) != 5 or set(ids) != set(EXPECTED_IDS):
            issues.append(dict(code='assigned_case_ids_mismatch', run=role, observed=ids))
    rc = manifests['repeat'].get('config', {})
    if rc.get('post_failure_target_budget') != 2:
        issues.append(dict(code='repeat_budget_not_two_post_failure'))
    if set(rc.get('policies', {})) != {POLICY}:
        issues.append(dict(code='repeat_policy_set_mismatch'))
    cases, physical_costs = [], []
    for cid in EXPECTED_IDS:
        row = dict(case_id=cid)
        for role, path in (('repeat', run), ('reference', reference_run)):
            data = load_case(path, cid)
            row[role] = inspect_case(data, cid, role)
            parent_snapshot = case_map(manifests[role]).get(cid)
            child_snapshot = case_map(data['manifest']).get(cid)
            row[role]['case_snapshot_matches_parent'] = equality(child_snapshot, parent_snapshot)['equal']
            row[role]['config_matches_parent'] = equality(data['manifest'].get('config'),
                                                          manifests[role].get('config'))['equal']
            for name in ('case_snapshot_matches_parent', 'config_matches_parent'):
                if row[role][name] is not True:
                    data['issues'].append(dict(code=name+'_unverified'))
            if role == 'repeat':
                physical_costs.append(event_cost(data, cid, prefix_only=False))
                unexpected = {e.get('policy_id') for e in data['episodes']} - {'shared', POLICY}
                if unexpected:
                    data['issues'].append(dict(code='unexpected_episode_policies', policies=sorted(map(str, unexpected))))
            issues.extend(dict(issue, case_id=cid, run=role) for issue in data['issues'])
        lhs = row['repeat']['initial_provenance']['initial_payload_sha256']
        rhs = row['reference']['initial_provenance']['initial_payload_sha256']
        row['initial_payload_hash_equal'] = lhs == rhs if lhs is not None and rhs is not None else None
        cases.append(row)
    summaries = {}
    for role in LABELS:
        rows = [c[role] for c in cases]
        summaries[role] = summarize(rows)
        summaries[role]['trigger_diagnostic'] = trigger_diagnostic(rows)
        summaries[role]['cost_with_shared_initial'] = sum_event_costs([r['cost_with_shared_initial'] for r in rows])
    c004 = next(c for c in cases if c['case_id'] == 'c004')
    old_success, new_success = c004['reference']['gold_success'], c004['repeat']['gold_success']
    reproduction = dict(case_id='c004', reference=c004['reference']['success'], repeat=c004['repeat']['success'],
                        success_reproduced=(new_success if old_success is True else None),
                        status=('reference_not_confirmed_success' if old_success is not True else
                                'unresolved' if new_success is None else 'reproduced' if new_success else 'not_reproduced_within_three'))
    compare = comparability(manifests['repeat'], manifests['reference'])
    audit_path = Path(protocol_audit) if protocol_audit else ROOT/'reports/evidence_repeat_protocol_audit_20261030.json'
    compare['protocol_audit'] = protocol_audit_summary(audit_path.resolve(), manifests['repeat'], reference_run)
    compare['reference_budget_prefix_replay'] = summarize_budget_replay(cases)
    outcomes_complete = all(s['asr_at_3']['unknown_or_error_outcomes'] == 0 for s in summaries.values())
    return dict(schema_version=1, created_at=datetime.now(timezone.utc).isoformat(),
        status='complete' if outcomes_complete else 'incomplete',
        validation_status=('verified' if compare['all_recorded_checks_equal'] and not issues
                           and compare['reference_budget_prefix_replay']['status'] != 'requires_review' else 'requires_review'),
        run=str(run), reference_run=str(reference_run),
        design=dict(policy_id=POLICY, expected_case_ids=list(EXPECTED_IDS), assigned_cases=5,
                    total_target_budget=3, post_failure_target_budget=2,
                    shared_initial_across_runs=False, fresh_generation_and_initial_requested=True,
                    fixed_denominator=True, causal_effect_estimated=False, external_llm_seed_controlled=False),
        summaries=summaries, cases=cases, c004_reproduction=reproduction, comparability=compare,
        new_physical_cost=sum_event_costs(physical_costs), repeat_preflight_cost=preflight_summary(run),
        reference_preflight_cost=preflight_summary(reference_run), issues=issues,
        limitations=[
            '这是相同五例、相同 Evidence 策略的重新生成及重新执行；新旧首轮不要求相同，不估计配对因果效应。',
            '旧组只截前三次，未整组重跑；预算记录有差异，已有前两次继续决策的预算等价性由离线重放单列核验。新旧 Payload 不同，不能当固定 Payload 重放。',
            '固定五例分母；错误、未完成或缺失结果保留 null。确认成功比例不是将未知结果判为失败，完整 ASR 在存在未知时为 null。',
            'seed=20261029 仅记录本地配置；不意味着外部生成模型或 Target 的随机采样种子受到控制。',
            '五例为既有开发样本，一次重复只能描述本次是否再次成功，不能证明稳定成功率或证据的因果收益。',
            '成本仅汇总完成事件对应的已知 usage；进行中请求和缺失 usage 不按零成本解释，预检单列。',
            'token 总数是日志字段加总，非精确计费或上下文长度；provider input/cache 字段可能有重叠语义，分项保留于 JSON，不推导金额或长度效果。',
        ])


def render_markdown(report):
    def pct(value):
        return 'null' if value is None else f'{100*value:.1f}%'
    def outcome(value):
        return '成功' if value is True else '有效失败' if value is False else 'null（错误/未完成）'
    def gold(value):
        return 'True' if value is True else 'False' if value is False else 'null'
    lines = ['# Evidence 策略五例重复运行', '',
        '口径：全新候选生成、全新首轮 Target，首轮后最多 2 次，合计 TotalASR@3；固定五例分母。', '',
        '| 运行 | 确认成功/5 | 确认成功比例 | 未知/错误 | 完整 ASR@3 |',
        '|---|---:|---:|---:|---:|']
    for role in LABELS:
        s = report['summaries'][role]['asr_at_3']
        lines.append(f"| {LABELS[role]} | {s['confirmed_successes']}/5 | {pct(s['confirmed_success_fraction'])} | {s['unknown_or_error_outcomes']} | {pct(s['complete_asr'])} |")
    c004 = report['c004_reproduction']
    phrases = {'reproduced': '本次再次成功', 'not_reproduced_within_three': '本次前三次内未重现成功',
               'unresolved': '本次尚不能判定是否重现', 'reference_not_confirmed_success': '旧运行成功状态未确认'}
    lines += ['', f"c004：**{phrases[c004['status']]}**。"]
    for role in LABELS:
        s = c004[role]
        detail = (f"第 {s['first_success_attempt']} 次成功；成功前已修订：{'是' if s['success_after_revision'] else '否'}；"
                  f"成功动作 {s['success_action']}" if s['first_success_attempt'] is not None else outcome(s['gold_success']))
        lines.append(f"- {LABELS[role]}：{detail}。")
    lines += ['', '## 每例 Gold / Trigger / 动作路径', '']
    for case in report['cases']:
        lines.append(f"### {case['case_id']}")
        lines.append('')
        for role in LABELS:
            row = case[role]
            path = ' → '.join(f"{s['overall_attempt']}. {s['action'] or '未执行'} [Gold={gold(s['gold'])}, Trigger={s['trigger'] or 'null'}]" for s in row['timeline'])
            lines.append(f"- {LABELS[role]}：{path}。结果：{outcome(row['gold_success'])}；状态 `{row['status']}`。")
        lines.append('')
    lines += ['## Gold=False、Trigger=T 的出现频率及后续动作', '',
              '| 运行 | 此类节点/有效失败 | 频率 | 仍有预算节点 | Retry | Revise | 无动作记录 |',
              '|---|---:|---:|---:|---:|---:|---:|']
    for role in LABELS:
        d = report['summaries'][role]['trigger_diagnostic']
        lines.append(f"| {LABELS[role]} | {d['gold_false_trigger_true_executions']}/{d['valid_failed_executions']} | {pct(d['frequency_among_valid_failures'])} | {d['eligible_nodes_with_budget']} | {d['eligible_retry_count']} | {d['eligible_revise_count']} | {d['eligible_nodes_without_recorded_action']} |")
    lines += ['', '有效失败分母只含前三次内 Gold=False 的结果。错误及缺失不进入该分母；只有第 1、2 次之后仍有本报告预算，旧运行第 3 次后的动作不计。', '',
              '## 可比性与成本', '', report['comparability']['interpretation'], '']
    comp = report['comparability']
    lines.append(f"- 配置、数据资源、case 快照检查：{'全部一致' if comp['all_recorded_checks_equal'] else '存在差异或缺失'}；差异项 {len(comp['mismatched_checks'])}，缺失项 {len(comp['unavailable_checks'])}。细项见 JSON。")
    replay = comp['reference_budget_prefix_replay']
    if replay['status'] == 'verified':
        lines.append(f"- 旧日志 {replay['decisions_replayed']} 次前缀继续决策已离线按旧预算和 B=2 重放，动作及理由均与原记录一致；当前固定 Controller 的前两次继续决策在这两个预算下相同，可按前三次口径比较。新旧 Payload 不同，不能当固定 Payload 重放。")
    else:
        lines.append(f"- 前缀预算重放状态 `{replay['status']}`：已重放 {replay['decisions_replayed']}/{replay['recorded_continue_decisions']} 次已记录继续决策；未确认的等价性不作推断。")
    audit = comp['protocol_audit']
    if audit['status'] != 'unavailable':
        link = f"[独立协议审计]({audit['source']})"
        lines.append(f"- {link}仅作为历史记录；是否对应参考运行：{audit.get('reference_manifest_matches')}。源码哈希已移除，该审计不认证当前实现。")
    cost, preflight = report['new_physical_cost'], report['repeat_preflight_cost']
    lines += [f"- 本次新增评分成本（含 shared P0/H1）：Target 已完成事件 {cost['target_completed_events']}、已保存结果 {cost['target_results_recorded']} 次；生成已完成事件 {cost['generation_completed_events']}、模型调用 {cost['generator_model_calls']} 次；token 日志字段加总 {cost['total_tokens_known']}（非精确计费/上下文长度），usage 完整：{cost['total_tokens_complete']}。",
              f"- 本次另计预检：调用 {preflight['calls_recorded']} 次，token 日志字段加总 {preflight['total_tokens_known']}（非精确计费/上下文长度）；不计入评分执行预算或上面的新增评分成本。",
              f"- 数据状态 `{report['status']}`；核验状态 `{report['validation_status']}`；日志问题 {len(report['issues'])} 项。完成事件、未完成请求、Target 秒数及观察查询成本见 JSON。", '',
              '## 解释边界', '']
    lines += ['- '+line for line in report['limitations']]
    lines += ['', f"本次目录：`{report['run']}`", '', f"参考目录：`{report['reference_run']}`", '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--reference-run', required=True)
    parser.add_argument('--outputprefix', required=True)
    parser.add_argument('--protocol-audit', help='Optional historical offline audit JSON; does not certify current code.')
    args = parser.parse_args(argv)
    report = analyze(args.run, args.reference_run, args.protocol_audit)
    prefix = Path(args.outputprefix)
    if prefix.suffix in ('.json', '.md'):
        prefix = prefix.with_suffix('')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    prefix.with_suffix('.md').write_text(render_markdown(report))
    print(json.dumps(dict(status=report['status'], output_json=str(prefix.with_suffix('.json')),
                         output_markdown=str(prefix.with_suffix('.md'))), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
