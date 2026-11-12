"""Apply the same reviewed infrastructure diff to verified v1/v2 source trees."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_asr_isolation import prepare

PATCHED = ['src/attacker/transport.py', 'src/attacker/session.py', 'src/vendor/refinement.py',
           'src/adapters/agentdojo.py', 'src/attacker/experience.py', 'src/attacker/aligned.py',
           'src/engine/config.py', 'src/attacker/initialization.py', 'scripts/run_evidence_parallel.py',
           'scripts/verify_a2_v2_test30_20261030.py']
ADDED = ['src/attacker/call_logging.py', 'src/attacker/accounting_proxy.py',
         'scripts/strict_asr_report.py', 'scripts/audit_asr_selection.py',
         'scripts/asr_interleaved_arm.py', 'scripts/audit_fresh_initializations.py',
         'scripts/export_fresh_initializations.py', 'scripts/asr_holdout_guard.py']


def build(label, original, output, selection, purpose='Fixed six-case reliability/behavior screen; no general ASR claim'):
    output = Path(output).resolve()
    lock = ROOT/'configs'/(original+'.lock.json')
    prepare(ROOT, ROOT/'runs'/(original+'_control'), lock, output)
    backup = ROOT/'runs/asr_goal_20261101/offline/before_reliability'
    patches = []
    for name in PATCHED:
        old, new = (backup/name).read_text(), (ROOT/name).read_text()
        difference = ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                fromfile=name, tofile=name))
        if difference:
            result = subprocess.run(['patch', '--batch', '--forward', '-p0', '-d', str(output)],
                                    input=difference, text=True, capture_output=True)
            patches.append(dict(path=name, returncode=result.returncode, output=result.stdout+result.stderr))
            if result.returncode:
                raise RuntimeError('Common infrastructure patch failed: '+name+'\n'+result.stdout+result.stderr)
    for name in ADDED:
        (output/name).write_bytes((ROOT/name).read_bytes())
    frozen = json.loads(lock.read_text())
    config = frozen['config']
    config['attacker']['rolling_experience'] = False
    exp = config['experiment']
    exp.update(assigned_case_ids=selection, rolling_experience='Disabled by attacker.rolling_experience=false in both conditions',
               online_evaluation_status='authorized_current_goal_bounded_protocol',
               source_snapshot=str(output), source_snapshot_manifest='source_manifest.json',
               condition=label, independent_heldout=False,
               purpose=purpose,
               case_parallelism=1)
    (output/'condition.lock.json').write_text(json.dumps(frozen, ensure_ascii=False, indent=2)+'\n')
    (output/'common_infrastructure_patch.json').write_text(json.dumps(patches, indent=2)+'\n')
    files = {str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest()
             for directory in ('src','scripts') for p in (output/directory).rglob('*')
             if p.is_file() and p.suffix in ('.py','.txt')}
    files['main.py'] = hashlib.sha256((output/'main.py').read_bytes()).hexdigest()
    (output/'source_manifest.json').write_text(json.dumps(dict(snapshot=str(output), files=files,
        original_control=str(ROOT/'runs'/(original+'_control')), common_infrastructure_only=True),indent=2)+'\n')
    print(json.dumps(dict(condition=label, root=str(output), source_files=len(files), api_calls=0)))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True)
    p.add_argument('--case-set', choices=('diagnostic','development'), default='diagnostic')
    args = p.parse_args()
    contract = json.loads((ROOT/'refine-logs/asr-improvement-20261101/GOAL_CONTRACT.json').read_text())
    for label, original in [('history_v1','a2_v2_terminal_history_test30_20261031_172750'),
                            ('plan_v2','a2_v2_plan_memory_test30_20261031_224926')]:
        selection = contract['diagnostic_case_ids'] if args.case_set=='diagnostic' else [f'c{i:03}' for i in range(30)]
        purpose = ('Fixed six-case reliability/behavior screen; no general ASR claim' if args.case_set=='diagnostic'
                   else 'Fixed 30-case development population; formal paired protocol separately preregistered')
        build(label, original, Path(args.output)/label, selection, purpose)
