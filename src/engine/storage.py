"""Exclusive run creation and durable records; completed runs are never overwritten."""
from dataclasses import asdict, is_dataclass
from pathlib import Path
import json
import os


def plain(value):
    if hasattr(value, 'model_dump'):
        return plain(value.model_dump(mode='json'))
    if is_dataclass(value):
        return plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(plain(value), stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


class RunStore:
    def __init__(self, path, manifest):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=False)
        write_json(self.path / 'manifest.json', manifest)
        (self.path / 'manifest.json').chmod(0o444)
        for name in ('episodes', 'decisions', 'events'):
            (self.path / (name + '.jsonl')).touch()

    def append(self, name, value):
        with (self.path / (name + '.jsonl')).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(plain(value), ensure_ascii=False, allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def raw(self, relative, value):
        write_json(self.path / 'raw' / relative, value)
        return 'raw/' + relative
