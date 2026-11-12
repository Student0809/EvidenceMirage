"""Opt-in DeepSeek environment setup. Never persist or return credentials."""
import os
from pathlib import Path

DEEPSEEK_URL = 'https://api.deepseek.com/anthropic'


def configure_deepseek(env_file=None):
    key = os.environ.get('DEEPSEEK_API_KEY')
    source = 'process:DEEPSEEK_API_KEY' if key else None
    if not key and os.environ.get('ANTHROPIC_BASE_URL', '').rstrip('/') == DEEPSEEK_URL:
        key = os.environ.get('ANTHROPIC_AUTH_TOKEN')
        if key:
            source = 'process:ANTHROPIC_AUTH_TOKEN'
    if not key and env_file is not None:
        from dotenv import dotenv_values
        values = dotenv_values(Path(env_file), interpolate=False)
        key = values.get('DEEPSEEK_API_KEY')
        if key:
            source = 'env_file:DEEPSEEK_API_KEY'
    if not key or key.startswith('<'):
        raise ValueError('Missing DeepSeek credential; configure DEEPSEEK_API_KEY or a DeepSeek-scoped ANTHROPIC_AUTH_TOKEN')
    os.environ['ANTHROPIC_AUTH_TOKEN'] = key
    for name in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):
        os.environ.pop(name, None)
    os.environ.update(
        ANTHROPIC_BASE_URL=DEEPSEEK_URL,
        ANTHROPIC_MODEL='deepseek-flash[1m]',
        ANTHROPIC_DEFAULT_OPUS_MODEL='deepseek-flash[1m]',
        ANTHROPIC_DEFAULT_SONNET_MODEL='deepseek-flash[1m]',
        ANTHROPIC_DEFAULT_HAIKU_MODEL='deepseek-flash',
        CLAUDE_CODE_SUBAGENT_MODEL='deepseek-flash',
        CLAUDE_CODE_EFFORT_LEVEL='max',
        CLAUDE_CODE_AUTO_COMPACT_WINDOW='786432',
    )
    return dict(endpoint=DEEPSEEK_URL, credential_source=source)
