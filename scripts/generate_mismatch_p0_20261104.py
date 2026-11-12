"""Generate one shared P0 per V5 seed, with no Target execution or outcome input."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentdojo.task_suite.load_suites import get_suite
from src.attacker.original import OriginalPIMinerAdapter
from src.attacker.transport import RoleDecision
from src.attacker.piminer import PUBLIC_FIELDS, GenerationError
from src.evidence.predicates.provider import configure_deepseek

MODEL = 'deepseek-flash[1m]'
SLOTS = ROOT / 'reports/mismatch_injection_slots_20261104/slots.json'
DATA = ROOT / 'Data/mismatch_dataset_v5'


def stamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(value if isinstance(value, str) else
                         json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def pointer_get(value, pointer):
    for piece in pointer.strip('/').split('/') if pointer else []:
        piece = piece.replace('~1', '/').replace('~0', '~')
        value = value[int(piece)] if isinstance(value, list) else value[piece]
    return value


def pointer_set(value, pointer, replacement):
    if not pointer:
        return replacement
    pieces = pointer.strip('/').split('/')
    node = value
    for piece in pieces[:-1]:
        piece = piece.replace('~1', '/').replace('~0', '~')
        node = node[int(piece)] if isinstance(node, list) else node[piece]
    key = pieces[-1].replace('~1', '/').replace('~0', '~')
    node[int(key) if isinstance(node, list) else key] = replacement
    return value


def prepare(out):
    inventory = json.loads(SLOTS.read_text())
    records = json.loads((DATA / 'dataset.json').read_text())
    seeds = {row['seed_id']: row for row in records}
    assert len(records) == 54 and len(seeds) == 18
    prepared = []
    for slot in inventory['slots']:
        sid = slot['seed_id']
        row = seeds[sid]
        for key in ('suite_name', 'user_task_prompt', 'injection_task_goal'):
            assert slot[key] == row[key], (sid, key)
        evidence = Path(slot['evidence_file'])
        assert digest(evidence) == slot['evidence_sha256'], sid
        trace = json.loads(evidence.read_text())['steps']
        index = slot['first_exposure_index_zero_based']
        assert trace[index]['call'] == slot['first_exposure_call'], sid
        assert [step['call'] for step in trace[:index]] == slot['preceding_calls'], sid
        original = deepcopy(trace[index]['result'])
        context = deepcopy(original)
        vector, = slot['injection_candidates']
        default = get_suite('v1.2.2', slot['suite_name']).get_injection_vector_defaults()[vector]
        # Native YAML defaults for banking contain literal backslash escapes.
        decoded_default = default.replace('\\t', '\t').replace('\\n', '\n')
        marker_re = re.compile(r'\[IPI_PLACEHOLDER[^\]]*\]')
        locations = []
        for occurrence in slot['visible_slot_occurrences']:
            pointer = occurrence['result_json_pointer']
            field = pointer_get(context, pointer)
            assert field == occurrence['frozen_field_text'], (sid, pointer)
            matches = list(marker_re.finditer(field))
            assert len(matches) == 1, (sid, pointer, 'placeholder count')
            marker = matches[0]
            start = marker.start()
            if default:
                candidates = [item for item in {default, decoded_default} if item in field[:start]]
                assert candidates, (sid, 'native default absent')
                selected = max(candidates, key=len)
                start = field.rfind(selected, 0, marker.start())
                gap = field[start + len(selected):marker.start()]
                assert not gap.replace('\\n', '').strip(), (sid, 'non-whitespace gap')
            native_marker = '{INJECTION:' + vector + '}'
            context = pointer_set(context, pointer, field[:start] + native_marker + field[marker.end():])
            locations.append(dict(pointer=pointer, native_variable=vector,
                                  variable_replaces_default=bool(default)))
        rendered = context if isinstance(context, str) else json.dumps(context, ensure_ascii=False, indent=2)
        assert not marker_re.search(rendered), sid
        assert rendered.count('{INJECTION:' + vector + '}') == len(locations), sid
        case = {key: deepcopy(slot[key]) for key in PUBLIC_FIELDS if key in slot}
        case.update(case_id=sid, context_with_placeholder=rendered)
        assert all(key in case for key in PUBLIC_FIELDS)
        provenance = dict(seed_id=sid, source_status=slot['source_status'],
                          original_pool_context_used=False, exposure_index=index,
                          evidence_sha256=digest(evidence), selected_locations=locations,
                          context_source='current frozen pre-exposure tool result only',
                          excluded=['pre/post state', 'later tools/results', 'normal/goal facts',
                                    'M1/M2/M3 replies', 'branch labels', 'gold/utility outcomes'])
        write(out / 'inputs' / f'{sid}.json', case)
        write(out / 'inputs' / f'{sid}.provenance.json', provenance)
        prepared.append((case, context, locations))
    assert len(prepared) == 18
    checks = dict(status='passed', seeds=18, shared_records=54,
                  selected_variable_occurrences=sum(len(x[2]) for x in prepared),
                  current_tasks_and_goals_match_v5=True, api_calls=0, target_runs=0)
    write(out / 'input_validation.json', checks)
    return prepared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--env-file', default='/home/zhengqingliu/data/PIMiner/.env')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'manifest.json').exists():
        raise SystemExit('Refusing to overwrite a dispatched run; use a fresh directory.')
    prepared = prepare(out)
    if args.prepare_only:
        print(json.dumps(json.loads((out / 'input_validation.json').read_text())))
        return
    provider = configure_deepseek(args.env_file)
    os.environ.update(PIMINER_CALL_LOG=str(out / 'calls.jsonl'),
                      PIMINER_STOP_FILE=str(out / 'STOP.json'),
                      PIMINER_RUN_ID=out.name)
    baseline = {str(p.relative_to(ROOT)): digest(p)
                for p in sorted(DATA.rglob('*')) if p.is_file()}
    manifest = dict(started_at=stamp(), pid=os.getpid(), model=MODEL,
                    effort='max', backend='Claude Code CLI', provider=provider,
                    workers=args.workers, target_model_routing_hint='gpt-5-nano',
                    target_model_runs=0, target_behavior_validated=False,
                    pipeline='original PIMiner Router -> Attacker, full strategy library',
                    source_sha256=digest(SLOTS), dataset_files=baseline,
                    code_sha256=digest(Path(__file__)),
                    policy='one P0 per seed, shared by all three mismatch records; no feedback')
    write(out / 'manifest.json', manifest)
    states = {case['case_id']: dict(stage='queued', updated_at=stamp()) for case, _, _ in prepared}
    completed = {}
    lock = threading.Lock()
    started = time.monotonic()

    def refresh():
        with lock:
            snapshot = deepcopy(states)
            results = deepcopy(completed)
        successes = sum(s['stage'] == 'completed' for s in snapshot.values())
        failures = sum(s['stage'] == 'error' for s in snapshot.values())
        done = successes + failures == 18
        status = dict(updated_at=stamp(), pid=os.getpid(),
                      status=('completed' if not failures else 'completed_with_errors') if done else 'running',
                      completed=successes, errors=failures, total=18,
                      elapsed_seconds=round(time.monotonic() - started), cases=snapshot,
                      target_runs=0, target_behavior_validated=False)
        write(out / 'status.json', status)
        write(out / 'P0.jsonl', ''.join(json.dumps(results[sid], ensure_ascii=False) + '\n' for sid in sorted(results)))
        lines = ['# 错配数据集 P0 生成进度', '',
                 f"更新时间（北京时间）：{status['updated_at']}", '',
                 f"状态：{status['status']}；成功 {successes}/18；错误 {failures}；用时 {status['elapsed_seconds']} 秒。", '',
                 'Claude Code + deepseek-flash[1m] / max；3 路并行；原 PIMiner Router → Attacker。',
                 '每个种子一份共享 P0。仅生成和原生变量替换校验；Target 执行数为 0，攻击有效性未测量。', '',
                 f'[已生成 P0 数据]({out}/P0.jsonl) · [输入校验]({out}/input_validation.json) · [调用记录]({out}/calls.jsonl)', '',
                 '| 种子 | 当前步骤 | 更新时间 | 结果 |', '|---|---|---|---|']
        for sid, state in sorted(snapshot.items()):
            link = f'[P0]({out}/cases/{sid}/P0.txt)' if sid in results else state.get('error_type', '')
            lines.append(f"| {sid} | {state['stage']} | {state['updated_at']} | {link} |")
        lines += ['', '生成输入仅包含当前用户任务、攻击目标、选定变量和首次暴露工具输出；不含冻结的后续工具执行、分支标签、回复或私有状态。',
                  'P0 替换所选原生变量，保留变量外正文与元数据；S05/S15 的同一变量同时出现在两条酒店评论中。',
                  '进度每 2 秒刷新。此对话的文件链接保持不变，打开可查看最新内容。', '']
        write(out / 'LIVE.md', '\n'.join(lines))

    class TrackedDecision(RoleDecision):
        def __init__(self, sid, system):
            super().__init__(MODEL, 'max', 900, system)
            self.sid = sid

        def __call__(self, prompt, stage):
            with lock:
                states[self.sid] = dict(stage=stage, updated_at=stamp())
            write(out / 'cases' / self.sid / f'{stage}.prompt.txt', prompt)
            write(out / 'cases' / self.sid / f'{stage}.system.txt', self.system_prompt)
            try:
                result = super().__call__(prompt, stage)
                write(out / 'cases' / self.sid / f'{stage}.output.txt', result)
                return result
            finally:
                write(out / 'cases' / self.sid / f'{stage}.call.json', self.calls[-1])

    def worker(item):
        case, context, locations = item
        sid = case['case_id']
        try:
            adapter = OriginalPIMinerAdapter(ROOT, MODEL, 'gpt-5-nano',
                      decide=TrackedDecision(sid, (ROOT / 'src/vendor/attacker_role.txt').read_text()),
                      route_decide=TrackedDecision(sid, ''))
            payload, record = adapter.generate_with_history(case, None, None, (), 'P0', ())
            write(out / 'cases' / sid / 'generation.json', record)
            raw = record['candidates'][-1]
            assert re.fullmatch(r'\s*<analysis>.*?</analysis>\s*<injection>.+?</injection>\s*', raw, re.S), 'Malformed attacker output'
            assert record['analysis'].strip() and payload.text.strip(), 'Empty blocks'
            assert not re.search(r'\[IPI_PLACEHOLDER|\{INJECTION:', payload.text), 'Unresolved placeholder'
            vector, = case['injection_candidates']
            instantiated = deepcopy(context)
            for location in locations:
                pointer = location['pointer']
                field = pointer_get(context, pointer)
                native_marker = '{INJECTION:' + vector + '}'
                assert field.count(native_marker) == 1
                replaced = field.replace(native_marker, payload.text)
                # Round trip establishes preservation of every byte outside the selected variable.
                before, after = field.split(native_marker)
                assert replaced == before + payload.text + after
                instantiated = pointer_set(instantiated, pointer, replaced)
            write(out / 'cases' / sid / 'P0.txt', payload.text)
            write(out / 'cases' / sid / 'instantiated_tool_result.json', instantiated)
            result = dict(seed_id=sid, shared_record_ids=[f'{sid}_M{i}' for i in (1, 2, 3)],
                          payload_id='P0', injection_candidates=case['injection_candidates'],
                          injection=payload.text, analysis=record['analysis'], sha256=payload.sha256,
                          routing=record['routing'], model=MODEL, effort='max',
                          slot_roundtrip_validated=True, target_behavior_validated=False)
            write(out / 'cases' / sid / 'result.json', result)
            with lock:
                completed[sid] = result
                states[sid] = dict(stage='completed', updated_at=stamp())
        except Exception as exc:
            # Persist structural errors; provider transport deliberately avoids raw stderr/secrets.
            error = dict(seed_id=sid, error_type=type(exc).__name__)
            if isinstance(exc, GenerationError):
                error['generation'] = exc.record
            elif isinstance(exc, AssertionError):
                error['validation_message'] = str(exc)
            write(out / 'cases' / sid / 'error.json', error)
            with lock:
                states[sid] = dict(stage='error', updated_at=stamp(), error_type=error['error_type'])

    refresh()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(worker, item) for item in prepared]
        while not all(future.done() for future in futures):
            refresh()
            time.sleep(2)
        for future in futures:
            future.result()
    unchanged = all((ROOT / p).exists() and digest(ROOT / p) == value for p, value in baseline.items())
    write(out / 'final_validation.json', dict(dataset_unchanged=unchanged,
          completed=len(completed), total=18, errors=18-len(completed), target_runs=0,
          target_behavior_validated=False, finished_at=stamp()))
    refresh()
    if len(completed) != 18 or not unchanged:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
