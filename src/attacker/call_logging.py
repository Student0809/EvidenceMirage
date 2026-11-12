"""Durable, prompt-free accounting for CLI invocations and Target HTTP calls.

CLI invocations are NOT HTTP counts. Unknown usage/cost remains None. A shared
stop file prevents new dispatches after a 402; already dispatched calls may end.
"""
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import uuid
import sys


class CallsStopped(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def stop_path():
    value = os.environ.get('PIMINER_STOP_FILE')
    return Path(value) if value else None


def check_stop():
    path = stop_path()
    if path and path.exists():
        raise CallsStopped('Shared experiment stop marker is set')


def stop_on_status(record):
    if record.get('status_code') != 402:
        return
    path = stop_path()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('x') as stream:
                json.dump(dict(reason='http_402', call_id=record.get('call_id'), at=now()), stream)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass


def numeric_tree(value):
    """Keep provider token/cost numbers without retaining free-form text."""
    if isinstance(value, dict):
        return {key: numeric_tree(item) for key, item in value.items()
                if isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_.\[\]/:-]{1,100}', key)
                and (isinstance(item, dict) or type(item) in (int, float))}
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return value
    return None


def journal(record, event, destination_path=None):
    path = destination_path or os.environ.get('PIMINER_CALL_LOG')
    if not path:
        return
    fields = ('call_id', 'kind', 'stage', 'model', 'status', 'started_at', 'finished_at',
              'seconds', 'status_code', 'error_type', 'returncode', 'dispatched',
              'usage', 'model_usage', 'cost_usd', 'reported_cost_usd', 'http_requests',
              'cli_max_retries', 'parent_call_id', 'run_id', 'usage_complete', 'persistence_errors')
    entry = {key: record.get(key) for key in fields}
    for key in ('usage', 'model_usage', 'cost_usd', 'reported_cost_usd'):
        entry[key] = numeric_tree(entry[key])
    entry.update(event=event, logged_at=now(), pid=os.getpid())
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def begin_call(record, kind):
    record.update(call_id=str(uuid.uuid4()), kind=kind, started_at=now(),
                  usage=None, model_usage=None, cost_usd=None, reported_cost_usd=None,
                  http_requests=None if kind == 'attacker_cli' else 0, dispatched=False)
    record.setdefault('run_id', os.environ.get('PIMINER_RUN_ID'))
    if kind == 'attacker_cli':
        record['cli_max_retries'] = 2
    check_stop()
    # This is an intent record; an interrupted dispatch has unknown billing.
    journal(record, 'intent')


def dispatch_call(record):
    check_stop()
    record['dispatched'] = True
    if record['kind'] in ('target_http', 'attacker_http'):
        record['http_requests'] = 1
    journal(record, 'dispatch')


def finish_call(record):
    record['finished_at'] = now()
    stop_on_status(record)
    try:
        journal(record, 'finish')
    except OSError as exc:
        error = dict(operation='finish_journal', errno=exc.errno, reason=exc.strerror)
        record.setdefault('persistence_errors', []).append(error)
        fallback = Path.home() / 'temp_run' / 'accounting_fallback' / f'{record["call_id"]}.jsonl'
        record['accounting_fallback'] = str(fallback)
        try:
            journal(record, 'finish', destination_path=str(fallback))
        except OSError as fallback_exc:
            record['persistence_errors'].append(dict(operation='fallback_journal',
                errno=fallback_exc.errno, reason=fallback_exc.strerror))
        print(f'Accounting persistence warning: errno={exc.errno}; fallback={fallback}', file=sys.stderr)



def capture_cli(record, output, stderr, returncode):
    """Parse partial usage even on nonzero exit; never persist output/stderr."""
    record['returncode'] = returncode
    try:
        result = json.loads(output)
    except (ValueError, TypeError):
        result = None
    if isinstance(result, dict):
        record.update(usage=numeric_tree(result.get('usage')),
                      model_usage=numeric_tree(result.get('modelUsage')),
                      reported_cost_usd=numeric_tree(result.get('total_cost_usd')))
    if returncode or (isinstance(result, dict) and result.get('is_error')):
        diagnostic = '\n'.join(str(v or '') for v in (output, stderr))
        match = re.search(r'(?:HTTP(?:/\d(?:\.\d)?)?\s+|API Error:\s*|["\']?status(?:_code)?["\']?\s*[:=]\s*)([45]\d\d)\b', diagnostic, re.I)
        if match:
            record['status_code'] = int(match.group(1))
        elif re.search(r'insufficient balance', diagnostic, re.I):
            record['status_code'] = 402
        stop_on_status(record)
    return result


def cli_environment(record=None, **extra):
    # Verified against the installed CLI using a loopback 429 provider. This
    # bounds internal retries; it does not make stage calls equal HTTP calls.
    env = dict(os.environ, CLAUDE_CODE_MAX_RETRIES='2',
               CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES='0', **extra)
    if record is not None and env.get('PIMINER_ACCOUNTING_PROXY') == '1':
        headers = env.get('ANTHROPIC_CUSTOM_HEADERS', '')
        env['ANTHROPIC_CUSTOM_HEADERS'] = headers + '\nX-PIMiner-Call-Id: ' + record['call_id'] + '\nX-PIMiner-Stage: ' + record['stage']
    return env
