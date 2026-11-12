"""Experiment A: three fixed historical P0s, two real entrypoints, five repetitions."""
import argparse
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from p0_repro_audit import HTTPAudit, record_pipeline, write_json, now

CASE_IDS = ('c007', 'c017', 'c028')
ARMS = ('original', 'current')
REPEATS = 5
ENDPOINT = 'https://api2.aigcbest.top/v1'
LOCK = ROOT / 'configs/evidence_piminer_aligned_asr5_20261030_v2.lock.json'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def current_environment():
    versions = {'python': sys.version, 'executable': sys.executable}
    for package in ('agentdojo', 'openai', 'httpx', 'pydantic', 'PyYAML', 'tenacity'):
        versions[package] = importlib.metadata.version(package)
    return versions


def make_schedule(case_ids=CASE_IDS, repeats=REPEATS):
    schedule = []
    for repeat in range(1, repeats + 1):
        for case_index, cid in enumerate(case_ids):
            arms = ARMS if (case_index + repeat) % 2 else tuple(reversed(ARMS))
            for position, arm in enumerate(arms):
                schedule.append(dict(trial_id=f'{cid}_{arm}_r{repeat:02}', case_id=cid,
                                     arm=arm, repeat=repeat, pair_position=position))
    return schedule


def configure_target():
    from dotenv import dotenv_values
    original = dotenv_values(ROOT.parent / 'PIMiner/.env', interpolate=False)
    fallback = dotenv_values(ROOT.parent / 'MiniPIM/.env', interpolate=False)
    key = (os.environ.get('OPENAI_API_KEY') or os.environ.get('PIMINER_TARGET_OPENAI_API_KEY')
           or original.get('OPENAI_API_KEY') or original.get('PIMINER_TARGET_OPENAI_API_KEY')
           or fallback.get('PIMINER_TARGET_OPENAI_API_KEY') or fallback.get('OPENAI_API_KEY'))
    if not key:
        raise RuntimeError('Existing Target credential is missing')
    os.environ['OPENAI_API_KEY'] = key
    os.environ['OPENAI_BASE_URL'] = ENDPOINT


def build_plan():
    import agentdojo
    from src.engine.config import check_frozen
    from src.attacker.initialization import InitializationBundle, public_identity
    from src.adapters.agentdojo import validate_native_cases
    lock = json.loads(LOCK.read_text())
    all_cases = check_frozen(lock)
    bundle = InitializationBundle(ROOT, lock['config']['attacker']['initialization_manifest'])
    cases = [c for c in all_cases if c['case_id'] in CASE_IDS]
    if tuple(c['case_id'] for c in cases) != CASE_IDS:
        raise ValueError('Fixed case set mismatch')
    historical = ROOT.parent / 'PIMiner/eval_results/pim_test/deepseek_claude_agentdojo_nano_20261023/iterative_attack_01_agentdojo_gpt-5-nano/samples'
    input_paths = {LOCK, ROOT / lock['config']['dataset'], ROOT / lock['config']['goal_specs'],
                   ROOT / lock['config']['attacker']['initialization_manifest']}
    for case in cases:
        initial = bundle.for_case(case)
        sample_path = historical / (case['case_id'][1:] + '.json')
        sample = json.loads(sample_path.read_text())
        if sample['history'][0]['injection'] != initial['payload'] or public_identity(sample) != public_identity(case):
            raise ValueError('Historical P0 or public identity mismatch')
        if digest(sample_path) != initial['source_metadata']['sample_sha256']:
            raise ValueError('Historical sample changed')
        case['p0'] = initial['payload']
        case['p0_sha256'] = hashlib.sha256(case['p0'].encode()).hexdigest()
        case['source_sample'] = str(sample_path.resolve())
        input_paths.add(sample_path)
    native = validate_native_cases(cases)
    source_paths = set((ROOT / 'src').rglob('*.py'))
    source_paths.update(ROOT / 'scripts' / name for name in
                        ('run_p0_stability.py', 'p0_repro_audit.py', 'p0_repro_original.py'))
    source_paths.update((ROOT.parent / 'PIMiner/benchmarks/agentdojo').rglob('*.py'))
    source_paths.add(ROOT.parent / 'PIMiner/iterative_attack_orchestrator/iterative_attack_common.py')
    source_paths.update(Path(agentdojo.__file__).parent.rglob('*.py'))
    versions = current_environment()
    input_hashes = {str(p.resolve()): digest(p) for p in sorted(input_paths)}
    return dict(schema_version=2, experiment='A_P0_stability', created_at=now(),
        target_endpoint=ENDPOINT, target_model='gpt-5-nano', benchmark_version='v1.2.2',
        cases=cases, schedule=make_schedule(), repeats_per_case_arm=5,
        top_level_executions=30, workers=3, input_hashes=input_hashes,
        source_paths=[str(p.resolve()) for p in sorted(source_paths)],
        environment=versions, environment_sha256=canonical_digest(versions),
        current_target=lock['config']['target'], original_root=str((ROOT.parent / 'PIMiner').resolve()),
        native_validation=native,
        protocol=dict(fresh_environment_per_top_level=True, original_internal_environment_reuse_preserved=True,
            same_current_dependencies=True, early_stop_on_success=False, replacement_after_error=False,
            attacker_calls=0, router_calls=0, extra_live_preflight_calls=0,
            order='One sequential worker per case; five adjacent original/current pairs, alternating first arm by case and repeat.',
            randomness='No deterministic Target seed is asserted. API default sampling and each entrypoint retry semantics are retained.',
            limits='30 scheduled top-level executions. Original may run up to 3 pipeline queries internally; current runs 1. HTTP sends are counted separately.',
            missingness='Exceptions/no final remain missing for comparison even if legacy entrypoint maps them to false.'),
        proxy_presence={k: bool(os.environ.get(k)) for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY')})


def verify_frozen(plan):
    # Legacy manifests mixed code and inputs. Code fingerprints are obsolete;
    # retain data/configuration checks without rewriting historical artifacts.
    inputs = {path: expected for path, expected in plan.get('source_hashes', {}).items()
              if Path(path).suffix != '.py'}
    inputs.update(plan.get('input_hashes', {}))
    for path, expected in inputs.items():
        if Path(path).suffix != '.py' and digest(path) != expected:
            raise ValueError('Frozen input changed: ' + path)
    if (canonical_digest(plan['environment']) != plan['environment_sha256']
            or current_environment() != plan['environment']):
        raise ValueError('Dependency environment changed')
    if plan['schedule'] != make_schedule() or plan['top_level_executions'] != 30:
        raise ValueError('Experiment A schedule changed')
    for case in plan['cases']:
        if hashlib.sha256(case['p0'].encode()).hexdigest() != case['p0_sha256']:
            raise ValueError('Frozen P0 changed')


def archive_sources(output, plan):
    """Keep source copies for inspection, without code fingerprint generation/checks."""
    output = Path(output)
    snapshot = output / 'source_snapshot'
    snapshot.mkdir()
    paths = plan.get('source_paths')
    if paths is None:
        paths = [path for path in plan.get('source_hashes', {}) if Path(path).suffix == '.py']
    copied = []
    for i, path in enumerate(paths):
        target = snapshot / (f'{i:04}_' + Path(path).name)
        shutil.copyfile(path, target)
        copied.append(dict(source=path, snapshot=str(target.relative_to(output))))
    write_json(output / 'source_snapshot.json', copied)


def summarize_results(results, schedule=None):
    schedule = make_schedule() if schedule is None else schedule
    expected = {r['trial_id']: r for r in schedule}
    if len(expected) != len(schedule):
        raise ValueError('Duplicate scheduled trial')
    by_id = {}
    for row in results:
        trial_id = row['trial_id']
        if trial_id not in expected or trial_id in by_id:
            raise ValueError('Unknown or duplicate trial result')
        if any(row[k] != expected[trial_id][k] for k in ('case_id','arm','repeat')):
            raise ValueError('Trial identity mismatch')
        by_id[trial_id] = row
    groups = []
    for cid in CASE_IDS:
        for arm in ARMS:
            assigned = [x for x in schedule if x['case_id']==cid and x['arm']==arm]
            rows = [by_id[x['trial_id']] for x in assigned if x['trial_id'] in by_id]
            valid = [x for x in rows if x.get('valid') is True and type(x.get('gold')) is bool and not x.get('error')]
            successes = sum(x['gold'] is True for x in valid)
            failures = sum(x['gold'] is False for x in valid)
            groups.append(dict(case_id=cid, arm=arm, assigned=len(assigned), completed=len(rows),
                successes=successes, failures=failures, errors=len(rows)-len(valid), pending=len(assigned)-len(rows),
                confirmed_fraction=successes/len(assigned),
                complete_success_rate=successes/len(assigned) if len(valid)==len(assigned) else None,
                http_send_attempts=sum(x.get('http',{}).get('http_send_attempts',0) for x in rows),
                transport_attempts=sum(x.get('http',{}).get('transport_attempts',0) for x in rows),
                sdk_create_calls=sum(x.get('sdk_create_calls',0) for x in rows),
                internal_queries=sum(x.get('internal_query_count',0) for x in rows)))
    return dict(updated_at=now(), assigned=len(schedule), completed=len(by_id),
        successes=sum(x['successes'] for x in groups), failures=sum(x['failures'] for x in groups),
        errors=sum(x['errors'] for x in groups), pending=len(schedule)-len(by_id), groups=groups,
        http_send_attempts=sum(x['http_send_attempts'] for x in groups),
        transport_attempts=sum(x['transport_attempts'] for x in groups),
        results=[by_id[x['trial_id']] for x in schedule if x['trial_id'] in by_id])


def execute_trial(case, spec, trial_dir, plan):
    from src.adapters.agentdojo import AgentDojoAdapter
    from src.schema import Payload
    with HTTPAudit(trial_dir, ENDPOINT) as audit:
        if spec['arm'] == 'original':
            from p0_repro_original import OriginalP0Runner
            runner = OriginalP0Runner(plan['original_root'], trial_dir, model='gpt-5-nano', reasoning_effort=None)
            output = runner.execute(case, case['p0'], spec['trial_id'])
            # The original wrapper explicitly separates legacy flags from valid scoring.
            valid = output.get('valid', output.get('audit_valid', False))
            gold = output.get('gold') if valid else None
            error = output.get('error') or output.get('execution_error')
            queries = output.get('internal_queries', [])
            query_count = len(queries) if isinstance(queries, list) else int(queries)
        else:
            target = AgentDojoAdapter(**plan['current_target'])
            with record_pipeline(target.pipeline, trial_dir) as queries:
                result = target.execute(case, Payload('P0', case['p0']), spec['trial_id'])
            output = asdict(result)
            valid = result.error is None and type(result.gold) is bool
            gold, error, query_count = result.gold, result.error, len(queries)
        write_json(trial_dir / 'raw_result.json', output)
    return dict(valid=bool(valid), gold=gold if valid else None,
                utility=output.get('utility') if valid else None, error=error if valid else (error or 'InvalidExecution'),
                internal_query_count=query_count, seconds=output.get('seconds'), http=audit.summary(),
                sdk_create_calls=len(output.get('api_calls',output.get('raw',{}).get('api_calls',[]))),
                payload_sha256=case['p0_sha256'])


def run_case_trials(case, schedule, output, plan, execute_fn=None):
    output = Path(output)
    execute_fn = execute_trial if execute_fn is None else execute_fn
    results = []
    for spec in schedule:
        if spec['case_id'] != case['case_id']:
            continue
        verify_frozen(plan)
        trial_dir = output / 'trials' / spec['trial_id']
        trial_dir.mkdir(parents=True, exist_ok=False)
        started = dict(spec, status='started', started_at=now(), pid=os.getpid(), payload_sha256=case['p0_sha256'])
        write_json(trial_dir / 'status.json', started)
        try:
            with (trial_dir / 'execution.log').open('x') as log, redirect_stdout(log), redirect_stderr(log):
                result = execute_fn(case, spec, trial_dir, plan)
        except Exception as exc:
            def started_rows(name):
                path = trial_dir / name
                return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
            sends = started_rows('http_events.jsonl')
            transport = started_rows('transport_events.jsonl')
            query_rows = started_rows('pipeline_queries.jsonl') + started_rows('original_pipeline_queries.jsonl')
            result = dict(valid=False, gold=None, utility=None, error=type(exc).__name__,
                internal_query_count=len({r['query_index'] for r in query_rows if 'query_index' in r}),
                http=dict(http_send_attempts=sum(r.get('status')=='started' for r in sends),
                          transport_attempts=sum(r.get('status')=='started' for r in transport)))
        row = dict(spec, **result, started_at=started['started_at'], completed_at=now())
        write_json(trial_dir / 'result.json', row)
        write_json(trial_dir / 'status.json', dict(started, status='completed', completed_at=row['completed_at'], valid=row['valid']))
        results.append(row)
    return dict(case_id=case['case_id'], completed=len(results))


def collect_results(output):
    return [json.loads(p.read_text()) for p in sorted((Path(output)/'trials').glob('*/result.json'))]


def write_report(output, summary):
    output = Path(output)
    rows = ['# 实验 A：固定 P0 复现稳定性', '',
        '同一当前环境、同一 Target 端点；3 个历史 P0 × 两个真实执行入口 × 各 5 次。', '',
        '| 案例 | 入口 | 成功/5 | 有效失败 | 错误 | 待完成 | 内部 pipeline | HTTP send/transport |',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for g in summary['groups']:
        rows.append(f"| {g['case_id']} | {g['arm']} | {g['successes']}/5 | {g['failures']} | {g['errors']} | {g['pending']} | {g['internal_queries']} | {g['http_send_attempts']}/{g['transport_attempts']} |")
    rows.extend(['', '错误保持未决，不当作有效失败，也不补跑。内部 pipeline 重入和 HTTP send 独立计数；HTTP send 包含 SDK 重试，transport 另计各次底层发送（含重定向）；两者均不等于服务端计费次数。', '',
        '这是固定 3 例的重复测量，每组仅 5 次；不用于推断完整 benchmark 的 ASR，也不证明历史服务端模型未变。', '',
        'manifest.json 固定了完整排程、P0、配置/数据与依赖环境；源码仅按路径归档副本，不生成或校验源码哈希。trials/* 中保存请求/响应 JSON、完整轨迹、原始结果与状态。', '',
        f"最后更新：{summary['updated_at']}", ''])
    (output/'report.md').write_text('\n'.join(rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError('Output already exists; started trials must never be silently repeated')
    plan = build_plan()
    verify_frozen(plan)
    if args.dry_run:
        print(json.dumps(dict(status='offline_valid', cases=list(CASE_IDS), trials=30, workers=3,
                              source_files=len(plan['source_paths']), input_files=len(plan['input_hashes']),
                              environment=plan['environment'], api_calls=0)))
        return 0
    configure_target()
    output.mkdir(parents=True, exist_ok=False)
    plan.update(pid=os.getpid(), started_at=now())
    write_json(output/'manifest.json', plan)
    archive_sources(output, plan)
    write_json(output/'status.json', dict(status='running', started_at=plan['started_at'], assigned=30, completed=0, pid=os.getpid()))
    failures = []
    last_completed = -1
    try:
        with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context('spawn')) as pool:
            futures = {pool.submit(run_case_trials, case, plan['schedule'], str(output), plan): case['case_id']
                       for case in plan['cases']}
            while futures:
                done, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
                for future in done:
                    cid = futures.pop(future)
                    try:
                        future.result()
                    except Exception as exc:
                        failures.append(dict(case_id=cid, error_type=type(exc).__name__))
                summary = summarize_results(collect_results(output), plan['schedule'])
                write_json(output/'summary.json', summary)
                write_json(output/'status.json', dict(status='running', pid=os.getpid(), started_at=plan['started_at'],
                    completed=summary['completed'], assigned=30, successes=summary['successes'], errors=summary['errors'],
                    updated_at=now(), worker_errors=failures))
                if summary['completed'] != last_completed:
                    print(json.dumps({k:summary[k] for k in ('updated_at','completed','successes','failures','errors','pending','http_send_attempts')}), flush=True)
                    write_report(output, summary)
                    last_completed = summary['completed']
        verify_frozen(plan)
        summary = summarize_results(collect_results(output), plan['schedule'])
        write_json(output/'summary.json', summary)
        write_report(output, summary)
        state = 'completed' if summary['completed']==30 and not failures else 'incomplete'
        write_json(output/'status.json', dict(status=state, started_at=plan['started_at'], completed_at=now(), assigned=30,
            completed=summary['completed'], successes=summary['successes'], errors=summary['errors'],
            worker_errors=failures, input_integrity='passed'))
        return 0 if state=='completed' else 1
    except BaseException as exc:
        write_json(output/'status.json', dict(status='interrupted', error_type=type(exc).__name__, updated_at=now(),
                   recovery='Do not repeat started trials; inspect durable trial and HTTP logs.'))
        raise


if __name__ == '__main__':
    raise SystemExit(main())
