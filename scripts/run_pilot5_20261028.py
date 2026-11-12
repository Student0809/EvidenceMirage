"""Launch the requested five-case pilot with the original PIMiner API routes.

Credentials are read as data into this process only, never copied or printed.
The current main.py owns all routing, generation, scoring, and retry decisions.
"""
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / 'PIMiner'
TARGET_ENV = ROOT.parent / 'MiniPIM/.env'
LOCK = ROOT / 'configs/pilot5_20261028.lock.json'
OUTPUT = ROOT / 'runs/pilot5_original_stage_aware_20261028'
LAUNCH = ROOT / 'reports/pilot5_launch_20261028.json'
sys.path.insert(0, str(ROOT))


def now():
    return datetime.now(timezone.utc).isoformat()


def launch():
    if OUTPUT.exists() or LAUNCH.exists():
        raise FileExistsError('This pilot was already launched; inspect its existing records')
    original = dotenv_values(SOURCE / '.env', interpolate=False)
    target = dotenv_values(TARGET_ENV, interpolate=False)
    target_key = (os.environ.get('OPENAI_API_KEY') or os.environ.get('PIMINER_TARGET_OPENAI_API_KEY')
                  or original.get('OPENAI_API_KEY') or original.get('PIMINER_TARGET_OPENAI_API_KEY')
                  or target.get('PIMINER_TARGET_OPENAI_API_KEY') or target.get('OPENAI_API_KEY'))
    attacker_key = os.environ.get('DEEPSEEK_API_KEY') or original.get('DEEPSEEK_API_KEY')
    if not target_key or not attacker_key:
        raise RuntimeError('Missing configured original Target or Attacker credential')
    os.environ['OPENAI_API_KEY'] = target_key
    os.environ['OPENAI_BASE_URL'] = 'https://api2.aigcbest.top/v1'
    os.environ['ANTHROPIC_AUTH_TOKEN'] = attacker_key
    for key in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):
        os.environ.pop(key, None)
    os.environ.update(ANTHROPIC_BASE_URL='https://api.deepseek.com/anthropic',
        ANTHROPIC_MODEL='deepseek-flash[1m]', ANTHROPIC_DEFAULT_OPUS_MODEL='deepseek-flash[1m]',
        ANTHROPIC_DEFAULT_SONNET_MODEL='deepseek-flash[1m]', ANTHROPIC_DEFAULT_HAIKU_MODEL='deepseek-flash',
        CLAUDE_CODE_SUBAGENT_MODEL='deepseek-flash', CLAUDE_CODE_EFFORT_LEVEL='max',
        CLAUDE_CODE_AUTO_COMPACT_WINDOW='786432')
    metadata = dict(status='preflight_then_run', started_at=now(), pid=os.getpid(),
        frozen_config=str(LOCK), output=str(OUTPUT),
        target_endpoint=os.environ['OPENAI_BASE_URL'], attacker_endpoint=os.environ['ANTHROPIC_BASE_URL'],
        credential_sources=[str(SOURCE / '.env'), str(TARGET_ENV)],
        source_wrapper=str(SOURCE / 'piminer_test_deepseek.sh'))
    LAUNCH.write_text(json.dumps(metadata, indent=2) + '\n')
    print('Starting five-case Stage-Aware pilot; API preflight precedes case execution.', flush=True)
    import main
    sys.argv = ['main.py', 'run', '--frozen', str(LOCK), '--output', str(OUTPUT)]
    try:
        main.main()
        metadata.update(status='completed', completed_at=now())
    except BaseException as exc:
        metadata.update(status='failed', completed_at=now(), error_type=type(exc).__name__)
        raise
    finally:
        LAUNCH.write_text(json.dumps(metadata, indent=2) + '\n')


if __name__ == '__main__':
    launch()
