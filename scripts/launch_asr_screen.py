"""Run the frozen six-case development screen, then stop for the ARIS gate.

This diagnostic runs arms sequentially. Formal acceptance requires a separate
predeclared interleaved repeated protocol, so this screen cannot complete Goal.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from run_observable_pilot5_20261029 import configure_credentials


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--protocol', required=True)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    protocol_path = Path(args.protocol).resolve()
    protocol = json.loads(protocol_path.read_text())
    output = Path(protocol['output']).resolve()
    if output.exists():
        raise FileExistsError('Screen output exists; never resume or overwrite')
    commands = []
    for arm in protocol['arms']:
        root, lock = Path(arm['source_root']), Path(arm['frozen'])
        if hashlib.sha256(lock.read_bytes()).hexdigest() != arm['frozen_sha256']:
            raise ValueError('Frozen screen lock changed')
        config = json.loads(lock.read_text())['config']
        if (config['post_failure_target_budget'] != 4
                or config['attacker'].get('rolling_experience') is not False
                or config['experiment']['assigned_case_ids'] != protocol['case_ids']):
            raise ValueError('Screen condition differs from protocol')
        command = [protocol['python'], str(root/'scripts/run_evidence_parallel.py'),
                   '--frozen', str(lock), '--output', str(output/arm['name']), '--workers', '1']
        # Always validate both isolated roots before starting any paid preflight.
        result = subprocess.run(command+['--dry-run'], cwd=root, text=True, capture_output=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        if result.returncode:
            raise RuntimeError('Offline condition check failed: '+result.stderr)
        commands.append((arm, command, json.loads(result.stdout)))
    if args.dry_run:
        print(json.dumps(dict(status='offline_valid', api_calls=0, conditions=[r for _,_,r in commands])))
        return
    if protocol.get('paid_execution_authorized') is not True:
        raise ValueError('Protocol lacks current paid-run authorization')
    configure_credentials()
    output.mkdir(parents=True, exist_ok=False)
    write(output/'protocol.json', protocol)
    status = dict(status='running', started_at=datetime.now(timezone.utc).isoformat(), arms=[])
    write(output/'status.json', status)
    env = dict(os.environ, DEEPSEEK_API_KEY=os.environ['ANTHROPIC_AUTH_TOKEN'],
               PIMINER_STOP_FILE=str(output/'STOP.json'), PYTHONDONTWRITEBYTECODE='1')
    env.pop('PIMINER_CALL_LOG', None)
    env.pop('PIMINER_ACCOUNTING_PROXY', None)
    for arm, command, check in commands:
        if (output/'STOP.json').exists():
            break
        row = dict(name=arm['name'], status='running', started_at=datetime.now(timezone.utc).isoformat(),
                   command=command, offline_check=check)
        status['arms'].append(row)
        write(output/'status.json',status)
        with (output/(arm['name']+'.launcher.log')).open('x') as log:
            process = subprocess.run(command, cwd=arm['source_root'],
                env=dict(env, PIMINER_RUN_ID=arm['name']), stdout=log, stderr=subprocess.STDOUT)
        row.update(status='process_finished', exit_code=process.returncode,
                   finished_at=datetime.now(timezone.utc).isoformat())
        write(output/'status.json',status)
        if process.returncode:
            break
    status.update(status='stopped_402' if (output/'STOP.json').exists() else 'awaiting_analysis',
                  finished_at=datetime.now(timezone.utc).isoformat())
    write(output/'status.json',status)
    print(json.dumps(status))


if __name__ == '__main__':
    main()
