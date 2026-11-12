"""Bounded 429 retries of an identical HTTP request, never a whole trajectory."""
import os
import random
import time


def rate_limit_retry(call, *args, **kwargs):
    retries = int(os.environ.get('PIMINER_RATE_LIMIT_RETRIES', '6'))
    base = float(os.environ.get('PIMINER_RATE_LIMIT_BASE', '2'))
    cap = float(os.environ.get('PIMINER_RATE_LIMIT_CAP', '60'))
    if retries < 0 or not 0 < base <= cap:
        raise ValueError('Invalid rate-limit retry configuration')
    for index in range(retries + 1):
        try:
            return call(*args, **kwargs)
        except Exception as exc:
            if getattr(exc, 'status_code', None) != 429 or index == retries:
                raise
            ceiling = min(cap, base * 2 ** index)
            delay = random.uniform(ceiling / 2, ceiling)
            headers = getattr(getattr(exc, 'response', None), 'headers', {})
            try:
                delay = max(delay, float(headers.get('retry-after', '0')))
            except (ValueError, TypeError):
                pass
            time.sleep(delay)
