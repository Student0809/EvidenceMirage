"""Copy a verified historical runtime and its inputs into a new standalone tree.

Offline only. Copies bytes, never links mutable active code into a comparison.
The original lock is retained as provenance, not as new online authorization.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(project, control, lock, output):
    project, control, lock, output = map(lambda p: Path(p).resolve(),
                                         (project, control, lock, output))
    if output.exists():
        raise FileExistsError('Isolation destination already exists')
    source_manifest = json.loads((control / 'source_manifest.json').read_text())
    frozen = json.loads(lock.read_text())
    sources = {}
    for relative, expected in source_manifest['files'].items():
        path = control / 'source' / relative
        if not path.resolve().is_relative_to((control / 'source').resolve()):
            raise ValueError('Source outside snapshot')
        if digest(path) != expected:
            raise ValueError('Historical source changed: ' + relative)
        sources[relative] = path
    for relative, expected in frozen['inputs_sha256'].items():
        if Path(relative).suffix == '.py':
            continue
        path = project / relative
        if not path.resolve().is_relative_to(project) or digest(path) != expected:
            raise ValueError('Frozen runtime input changed: ' + relative)
        if relative in sources and digest(sources[relative]) != expected:
            raise ValueError('Snapshot/input collision: ' + relative)
        sources[relative] = path
    output.mkdir(parents=True, exist_ok=False)
    for relative, path in sources.items():
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    shutil.copyfile(lock, output / 'historical.lock.json')
    result = dict(status='isolated_not_online_authorized', api_calls=0,
                  original_control=str(control), original_lock=str(lock),
                  snapshot_files_checked=len(source_manifest['files']),
                  runtime_inputs_checked=len(frozen['inputs_sha256']),
                  files={p: digest(output / p) for p in sorted(sources)},
                  historical_lock_sha256=digest(output / 'historical.lock.json'),
                  output=str(output))
    (output / 'isolation.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('project', 'control', 'lock', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    result = prepare(args.project, args.control, args.lock, args.output)
    print(json.dumps({k: v for k, v in result.items() if k != 'files'}))
