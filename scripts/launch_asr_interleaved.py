"""Execute a frozen paired schedule without restarting or repeating any case.

One preflight per arm/repeat; sequential paired cases alternate arm order.
Protocol is an explicit JSON artifact produced before any result is observed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from run_observable_pilot5_20261029 import configure_credentials
from asr_holdout_guard import CLAIM_ENV, cohort_spec, ensure_unclaimed, claim_cohort


def schedule(case_ids, names, repeats, seed):
    if len(names)!=2 or len(set(names))!=2 or len(set(case_ids))!=len(case_ids) or not case_ids:
        raise ValueError('Need two different arms and unique nonempty case IDs')
    if type(repeats) is not int or repeats < 1:
        raise ValueError('Repeats must be a positive integer')
    if any(not isinstance(name, str) or not name or Path(name).name != name or name in ('.', '..') for name in names):
        raise ValueError('Arm names must be single directory names')
    rng = random.Random(seed)
    rows = []
    for repeat in range(repeats):
        cases = list(case_ids); rng.shuffle(cases)
        for index,cid in enumerate(cases):
            order = names if (index+repeat)%2==0 else list(reversed(names))
            rows.extend(dict(repeat=repeat,case_id=cid,arm=name) for name in order)
    return rows


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.replace(path)


def response(process):
    line = process.stdout.readline()
    if not line:
        raise RuntimeError('Arm process ended without a response')
    return json.loads(line)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--protocol',required=True)
    p.add_argument('--dry-run',action='store_true')
    args = p.parse_args()
    protocol_bytes = Path(args.protocol).read_bytes()
    protocol = json.loads(protocol_bytes)
    output = Path(protocol['output']).resolve()
    if output.exists():
        raise FileExistsError(output)
    arms = {a['name']:a for a in protocol['arms']}
    if len(arms) != len(protocol['arms']):
        raise ValueError('Repeated arm name')
    expected = schedule(protocol['case_ids'],list(arms),protocol['repeats'],protocol['schedule_seed'])
    if expected != protocol['schedule']:
        raise ValueError('Preregistered schedule differs from seeded paired rule')
    checks = []
    for name,arm in arms.items():
        lock = Path(arm['frozen']);root = Path(arm['source_root'])
        if hashlib.sha256(lock.read_bytes()).hexdigest()!=arm['frozen_sha256']:
            raise ValueError('Frozen lock changed')
        if hashlib.sha256((root/'source_manifest.json').read_bytes()).hexdigest()!=arm['source_manifest_sha256']:
            raise ValueError('Frozen source manifest changed')
        command=[protocol['python'],str(root/'scripts/asr_interleaved_arm.py'),'--frozen',str(lock),
                 '--output',str(output/'repeat0'/name),'--dry-run']
        result=subprocess.run(command,cwd=root,capture_output=True,text=True)
        if result.returncode:
            raise RuntimeError('Offline arm validation failed: '+result.stderr)
        check=json.loads(result.stdout)
        if check['case_ids']!=protocol['case_ids']:
            raise ValueError('Frozen case population differs from schedule')
        checks.append(check)
    cohort = cohort_spec(protocol)
    if args.dry_run:
        print(json.dumps(dict(status='offline_valid',api_calls=0,checks=checks,scheduled_cases=len(expected))))
        return
    if protocol.get('paid_execution_authorized') is not True:
        raise ValueError('Missing current bounded-protocol authorization')
    if cohort is not None:
        ensure_unclaimed(ROOT, cohort)
    configure_credentials()
    output.mkdir(parents=True,exist_ok=False)
    claim = None
    if cohort is not None:
        # Preserve the exact reviewed protocol bytes bound by the cohort claim.
        (output/'protocol.json').write_bytes(protocol_bytes)
        claim = claim_cohort(ROOT, protocol, protocol_bytes, cohort)
    else:
        write(output/'protocol.json',protocol)
    state=dict(status='running',started_at=datetime.now(timezone.utc).isoformat(),completed_schedule=[])
    write(output/'status.json',state)
    env=dict(os.environ,DEEPSEEK_API_KEY=os.environ['ANTHROPIC_AUTH_TOKEN'],
             PIMINER_STOP_FILE=str(output/'STOP.json'),PYTHONDONTWRITEBYTECODE='1')
    env.pop('PIMINER_ACCOUNTING_PROXY',None);env.pop('PIMINER_CALL_LOG',None)
    for key in CLAIM_ENV:
        env.pop(key, None)
    processes,logs={},[]
    try:
        for repeat in range(protocol['repeats']):
            directory=output/('repeat'+str(repeat));directory.mkdir()
            processes={}
            for name,arm in arms.items():
                if (output/'STOP.json').exists():
                    raise RuntimeError('Shared 402 stop')
                root=Path(arm['source_root'])
                log=(directory/(name+'.launcher.log')).open('x');logs.append(log)
                child_env = dict(env,PIMINER_RUN_ID=f'repeat{repeat}/{name}')
                if claim is not None:
                    child_env.update(PIMINER_HELDOUT_CLAIM=str(claim),
                        PIMINER_HELDOUT_ARM=name, PIMINER_HELDOUT_REPEAT=str(repeat))
                process=subprocess.Popen([protocol['python'],'-u',str(root/'scripts/asr_interleaved_arm.py'),
                    '--frozen',arm['frozen'],'--output',str(directory/name)],cwd=root,
                    env=child_env,stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,stderr=log,text=True,bufsize=1)
                processes[name]=process
                if response(process)!={'status':'ready'}:
                    raise RuntimeError('Arm did not finish preflight')
            for row in [r for r in expected if r['repeat']==repeat]:
                if (output/'STOP.json').exists():
                    raise RuntimeError('Shared 402 stop')
                process=processes[row['arm']]
                process.stdin.write(json.dumps(dict(action='run',case_id=row['case_id']))+'\n');process.stdin.flush()
                result=response(process)
                if result.get('case_id')!=row['case_id']:
                    raise ValueError('Arm returned another case')
                state['completed_schedule'].append(dict(**row,result=result))
                write(output/'status.json',state)
            for process in processes.values():
                process.stdin.write('{"action":"finish"}\n');process.stdin.flush()
                final=response(process)
                if final.get('status') not in ('completed','completed_with_errors') or process.wait()!=0:
                    raise RuntimeError('Arm finalization failed')
            processes={}
        state['status']='awaiting_analysis'
    except BaseException as exc:
        state.update(status='stopped_402' if (output/'STOP.json').exists() else 'interrupted',error_type=type(exc).__name__)
        raise
    finally:
        # Closing idle stdin lets the arm archive an interrupted status. Never
        # restart or reissue a started case; unknown in-flight usage stays so.
        for process in processes.values():
            if process.poll() is None:
                process.stdin.close()
        for log in logs:log.close()
        state['finished_at']=datetime.now(timezone.utc).isoformat()
        write(output/'status.json',state)


if __name__=='__main__':
    main()
