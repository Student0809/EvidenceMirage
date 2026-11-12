"""A frozen arm process: preflight once, then execute preregistered cases on command.

Used by launch_asr_interleaved.py. Each case still uses the existing run_case
implementation and fresh adapters. This does not alter the attack method.
"""
import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys

from run_evidence_parallel import (ROOT, load_data_manifest, validate_protocol,
    initialization_metadata, execution_limits, run_case, configure_credentials, now, write_json)
from asr_holdout_guard import verify_child_claim, evaluation_metadata


def emit(value):
    print(json.dumps(value), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--frozen', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    output, frozen = Path(args.output).resolve(), Path(args.frozen).resolve()
    manifest = load_data_manifest(frozen)
    cases, _ = validate_protocol(manifest)
    config = manifest['config']
    if config['attacker'].get('rolling_experience') is not False:
        raise ValueError('Interleaved arms require disabled rolling experience')
    if config['post_failure_target_budget'] != 4:
        raise ValueError('Expected five Target executions per case')
    if output.exists():
        raise FileExistsError(output)
    from src.adapters.agentdojo import validate_native_cases
    native = validate_native_cases(cases, config['target']['benchmark_version'])
    if args.dry_run:
        emit(dict(status='offline_valid',api_calls=0,case_ids=[c['case_id'] for c in cases]))
        return
    claim = verify_child_claim(ROOT, frozen, manifest, [c['case_id'] for c in cases], output)
    configure_credentials()
    output.mkdir(parents=True, exist_ok=False)
    os.environ['PIMINER_CALL_LOG'] = str(output/'calls.jsonl')
    os.environ.setdefault('PIMINER_STOP_FILE',str(output.parent/'STOP.json'))
    launch = dict(manifest,cases=cases,case_ids=[c['case_id'] for c in cases],
        status='preflight',created_at=now(),pid=os.getpid(),workers=1,
        policy_id=next(iter(config['policies'])),**initialization_metadata(config),
        **evaluation_metadata(config, claim), learned_controller_enabled=False,
        total_target_budget=5,limits=execution_limits(config,len(cases)),
        target_endpoint=os.environ['OPENAI_BASE_URL'],attacker_endpoint=os.environ['ANTHROPIC_BASE_URL'],
        randomness_note='Harness schedule is seeded; external model randomness is not controlled.')
    write_json(output/'manifest.json',launch)
    write_json(output/'status.json',dict(status='preflight',started_at=launch['created_at']))
    from src.attacker.accounting_proxy import AccountingProxy
    proxy = AccountingProxy(os.environ['ANTHROPIC_BASE_URL'])
    os.environ['ANTHROPIC_BASE_URL'] = proxy.start()
    os.environ['PIMINER_ACCOUNTING_PROXY'] = '1'
    completed, results = set(), []
    try:
        from main import adapters
        with redirect_stdout(sys.stderr):
            target, attacker = adapters(config)
            write_json(output/'preflight.json',dict(native=native,target=target.preflight(),attacker=attacker.preflight()))
            del target, attacker
        write_json(output/'status.json',dict(status='ready',started_at=launch['created_at']))
        emit(dict(status='ready'))
        for line in sys.stdin:
            command = json.loads(line)
            if command == {'action':'finish'}:
                if completed != set(launch['case_ids']):
                    raise ValueError('Cannot finish with missing assigned cases')
                break
            if set(command) != {'action','case_id'} or command['action'] != 'run':
                raise ValueError('Invalid case command')
            cid = command['case_id']
            if cid not in launch['case_ids'] or cid in completed:
                raise ValueError('Foreign or repeated case')
            from src.attacker.call_logging import check_stop
            check_stop()
            write_json(output/'status.json',dict(status='running',case_id=cid,completed_cases=len(completed)))
            try:
                result = run_case(str(frozen),cid,str(output/cid))
            except Exception as exc:
                result = dict(case_id=cid,status='worker_error',error_type=type(exc).__name__)
            completed.add(cid); results.append(result)
            write_json(output/'worker_results.json',results)
            emit(result)
        else:
            raise RuntimeError('Parent closed command channel before finish')
        state = 'completed' if all(r['status']=='completed' for r in results) else 'completed_with_errors'
        write_json(output/'status.json',dict(status=state,completed_at=now(),selected_cases=len(cases),
            completed_workers=sum(r['status']=='completed' for r in results)))
        emit(dict(status=state))
    except BaseException as exc:
        write_json(output/'status.json',dict(status='interrupted',error_type=type(exc).__name__,completed_at=now()))
        raise
    finally:
        proxy.close()


if __name__ == '__main__':
    main()
