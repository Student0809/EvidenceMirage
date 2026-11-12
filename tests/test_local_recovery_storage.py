import errno
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from src.attacker.call_logging import finish_call
from src.mismatch.recovery import publish, recover


def test_finish_io_failure_does_not_override_success(tmp_path):
    record = dict(call_id='test', status='completed')
    from src.attacker.call_logging import journal
    def fail_primary(record, event, destination_path=None):
        if destination_path is None:
            raise OSError(errno.EIO, 'Input/output error')
        return journal(record, event, destination_path)
    with patch('src.attacker.call_logging.journal', side_effect=fail_primary), patch.object(Path, 'home', return_value=tmp_path):
        finish_call(record)
    assert record['status'] == 'completed'
    assert record['persistence_errors'][0]['errno'] == errno.EIO
    saved = json.loads(Path(record['accounting_fallback']).read_text())
    assert saved['event'] == 'finish'
    assert saved['status'] == 'completed'


def test_publish_verified_and_no_overwrite(tmp_path):
    local = tmp_path/'local'
    local.mkdir()
    (local/'result').write_text('original')
    final = tmp_path/'final'
    publish(local, final)
    assert (final/'result').read_text() == 'original'
    assert local.exists()
    with pytest.raises(FileExistsError):
        publish(local, final)


def test_local_run_copies_only_after_completion(tmp_path):
    args = SimpleNamespace(source=tmp_path/'source', output=tmp_path/'final'/'run',
                           local_root=tmp_path/'local', dry_run=False)
    def execute(options):
        assert options.output == tmp_path/'local'/'run'
        assert not args.output.exists()
        options.output.mkdir(parents=True)
        (options.output/'status.json').write_text('{"status":"completed"}')
    with patch('src.mismatch.recovery._recover', side_effect=execute):
        recover(args)
    assert (args.output/'status.json').exists()
    assert (tmp_path/'local'/'run'/'status.json').exists()
