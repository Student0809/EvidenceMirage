"""Verify preserved data/resource bytes without loading any model or reading credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.engine.storage import write_json


def verify(compare_source=False):
    manifest=json.loads((ROOT/'data/assets_manifest.json').read_text())
    rows=[]
    for asset in manifest['assets']:
        if Path(asset['path']).suffix == '.py':
            continue
        actual=hashlib.sha256((ROOT/asset['path']).read_bytes()).hexdigest()
        expected=asset.get('destination_sha256',asset['sha256'])
        if actual != expected:
            raise ValueError('Preserved asset changed: '+asset['path'])
        row=dict(path=asset['path'],sha256=actual,verified=True)
        if compare_source:
            source=Path(asset['source'])
            src=hashlib.sha256(source.read_bytes()).hexdigest()
            original=asset.get('source_sha256',asset['sha256'])
            if src != original:
                raise ValueError('Original source has changed since migration: '+str(source))
            row['original_source_unchanged']=True
        rows.append(row)
    return dict(status='passed',verified_assets=len(rows),source_compared=compare_source,rows=rows)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--compare-source',action='store_true')
    p.add_argument('--output',default=str(ROOT/'reports/asset_verification.json'))
    args=p.parse_args(); report=verify(args.compare_source);write_json(args.output,report)
    print(json.dumps({k:v for k,v in report.items() if k!='rows'}))
