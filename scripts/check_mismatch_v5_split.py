"""Reject train/dev/test assignments that split seeds, mechanisms or identical replies."""
import argparse,json
from pathlib import Path
from revise_mismatch_all18_v5_20261104 import OUT,split_errors

def main():
 p=argparse.ArgumentParser();p.add_argument('assignment',type=Path,help='JSON object instance_id -> train/dev/test (or other partition name)');p.add_argument('--instances',type=Path,default=OUT/'response_evaluations.json');p.add_argument('--allow-partial',action='store_true');a=p.parse_args()
 xs=json.loads(a.instances.read_text());assignment=json.loads(a.assignment.read_text());ids={x['instance_id'] for x in xs}
 if not isinstance(assignment,dict) or any(not isinstance(v,str) or not v for v in assignment.values()):p.error('assignment must map instance IDs to nonempty partition names')
 if set(assignment)-ids:p.error('assignment contains unknown instance IDs')
 if not a.allow_partial and set(assignment)!=ids:p.error('assignment must cover every instance, or explicitly use --allow-partial')
 errors=split_errors(xs,assignment)
 print(json.dumps(dict(status='rejected' if errors else 'passed',assigned_instances=len(assignment),leakage_keys=errors),indent=2))
 raise SystemExit(1 if errors else 0)
if __name__=='__main__':main()
