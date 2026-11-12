"""Run the frozen PIMiner test experiment, then write its offline verification.

This supervisor never resumes, replaces, or repeats failed model executions.
It can be detached by the caller; child logs and final reports are durable.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--report-prefix', required=True)
    parser.add_argument('--coverage', required=True)
    parser.add_argument('--workers', type=int, default=16)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    prefix = Path(args.report_prefix).resolve()
    if output.exists():
        raise FileExistsError('Run exists; automatic resume is disabled')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    log_path = prefix.with_suffix('.run.log')
    started = datetime.now(timezone.utc).isoformat()
    with log_path.open('x') as log:
        run_command = [sys.executable, '-u', str(ROOT / 'scripts/run_evidence_parallel.py'),
            '--frozen', str(Path(args.frozen).resolve()), '--output', str(output),
            '--workers', str(args.workers)]
        result = subprocess.run(run_command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        run_code = result.returncode
        # Even interrupted/errored runs retain their selected denominator.
        summary_code = None
        if (output / 'manifest.json').exists():
            summary_command = [sys.executable, str(ROOT / 'scripts/summarize_full_evidence.py'),
                '--run', str(output), '--output-prefix', str(prefix),
                '--coverage', str(Path(args.coverage).resolve()), '--verify-completed']
            summary_code = subprocess.run(summary_command, cwd=ROOT,
                stdout=log, stderr=subprocess.STDOUT).returncode
    record = dict(started_at=started, completed_at=datetime.now(timezone.utc).isoformat(),
        run_returncode=run_code, summary_returncode=summary_code, run=str(output),
        log=str(log_path), report_prefix=str(prefix))
    prefix.with_suffix('.supervisor.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record), flush=True)
    return run_code or summary_code or 0


if __name__ == '__main__':
    raise SystemExit(main())
