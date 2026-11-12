"""Descriptive Trigger statistics; these estimates must never drive a policy.

The posterior uses an exchangeable Bernoulli working model, conditional on
episodes with a known Trigger and no execution/observation error. It does not
establish independent sampling, correct missingness bias, or estimate Gold.
"""
import json
from math import exp, fsum, lgamma, log, log1p
from typing import Iterable

from ..schema import Episode


def _beta_cdf(x: float, alpha: int, beta: int) -> float:
    """Integer-shape Beta CDF via a binomial upper tail, using log masses."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    n = alpha + beta - 1
    coefficient = lgamma(n + 1)
    lx, ly = log(x), log1p(-x)
    value = fsum(exp(coefficient - lgamma(k + 1) - lgamma(n - k + 1)
                     + k * lx + (n - k) * ly)
                 for k in range(alpha, n + 1))
    return min(1.0, max(0.0, value))


def _beta_quantile(probability: float, alpha: int, beta: int) -> float:
    # Symmetry avoids subtracting nearly equal tail masses in the CDF.
    if probability > 0.5:
        return 1.0 - _beta_quantile(1.0 - probability, beta, alpha)
    lower, upper = 0.0, 1.0
    for _ in range(64):
        midpoint = (lower + upper) / 2
        if _beta_cdf(midpoint, alpha, beta) < probability:
            lower = midpoint
        else:
            upper = midpoint
    return (lower + upper) / 2


def trigger_statistics(episodes: Iterable[Episode]) -> dict:
    """Summarize one case/payload in one policy's view, including shared H1.

    Exact duplicate episode IDs count once; conflicting duplicates are invalid.
    Different cases, payload hashes, or non-shared policy branches cannot mix.
    nT/nF/nU exclude both kinds of error. The two error counts can overlap, as
    recorded in both_errors. Gold counts exclude Target errors only. A missing
    Gold verdict is an invalid Target execution, never a negative observation.
    Empty input returns the unchanged Beta(1, 1) prior and null observed rates.
    """
    unique = {}
    duplicate_count = 0
    for episode in episodes:
        if not isinstance(episode, Episode):
            raise TypeError('trigger_statistics requires Episode records')
        for name in ('episode_id', 'case_id', 'policy_id', 'payload_sha256'):
            value = getattr(episode, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f'Invalid {name}')
        if episode.trigger not in ('T', 'F', 'U'):
            raise ValueError('Invalid Trigger value')
        if episode.gold is not None and type(episode.gold) is not bool:
            raise ValueError('Gold must be boolean or null')
        previous = unique.get(episode.episode_id)
        if previous is not None:
            if previous != episode:
                raise ValueError('Conflicting duplicate episode_id')
            duplicate_count += 1
            continue
        unique[episode.episode_id] = episode

    rows = tuple(unique.values())
    case_ids = {e.case_id for e in rows}
    payload_hashes = {e.payload_sha256 for e in rows}
    policy_ids = {e.policy_id for e in rows}
    if len(case_ids) > 1:
        raise ValueError('Cannot mix cases')
    if len(payload_hashes) > 1:
        raise ValueError('Cannot mix payload hashes')
    if len(policy_ids - {'shared'}) > 1:
        raise ValueError('Cannot mix policy branches')

    counts = dict(nT=0, nF=0, nU=0, target_errors=0, observation_errors=0,
                  both_errors=0, gold_successes=0, gold_failures=0)
    updated_ids = []
    excluded = dict(target_error=[], observation_error=[], unknown_trigger=[])
    for episode in rows:
        target_error = (episode.error is not None or episode.gold is None
                        or episode.status == 'TARGET_ERROR' or episode.stage == 'ERROR')
        observation_error = episode.observation_error is not None
        counts['target_errors'] += int(target_error)
        counts['observation_errors'] += int(observation_error)
        counts['both_errors'] += int(target_error and observation_error)
        if target_error:
            excluded['target_error'].append(episode.episode_id)
        else:
            counts['gold_successes' if episode.gold else 'gold_failures'] += 1
        if observation_error:
            excluded['observation_error'].append(episode.episode_id)
        if target_error or observation_error:
            continue
        counts['n' + episode.trigger] += 1
        if episode.trigger == 'U':
            excluded['unknown_trigger'].append(episode.episode_id)
        else:
            updated_ids.append(episode.episode_id)

    alpha, beta = 1 + counts['nT'], 1 + counts['nF']
    total, known_trigger = len(rows), counts['nT'] + counts['nF']
    valid_gold = counts['gold_successes'] + counts['gold_failures']
    return dict(
        schema_version=1,
        case_id=next(iter(case_ids), None),
        payload_sha256=next(iter(payload_hashes), None),
        policy_ids=sorted(policy_ids),
        episode_ids=list(unique), total=total,
        duplicate_records_ignored=duplicate_count,
        **counts,
        gold_missing=total - valid_gold,
        updated_episode_ids=updated_ids,
        excluded_episode_ids=excluded,
        prior=dict(alpha=1, beta=1),
        alpha=alpha, beta=beta,
        mean=alpha / (alpha + beta),
        var=alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1)),
        credible_interval_95=dict(
            lower=_beta_quantile(0.025, alpha, beta),
            upper=_beta_quantile(0.975, alpha, beta),
            level=0.95, method='equal_tail'),
        observed_trigger_rate=counts['nT'] / known_trigger if known_trigger else None,
        observed_gold_success_rate=counts['gold_successes'] / valid_gold if valid_gold else None,
        coverage=dict(known_trigger_episodes=known_trigger, total_episodes=total,
                      fraction=known_trigger / total if total else None,
                      valid_gold_episodes=valid_gold,
                      gold_fraction=valid_gold / total if total else None),
        trigger_count_semantics='nT/nF/nU exclude Target and observation errors; error counts overlap only in both_errors.',
        posterior_scope='Trigger probability conditional on known, error-free observations; exchangeable Bernoulli working model, not an independence guarantee or Gold probability.',
        reporting_only=True, used_for_decisions=False,
    )


def trigger_statistics_report(episodes: Iterable[Episode]) -> dict:
    """Best-effort reporting: a statistics failure cannot change execution."""
    try:
        result = dict(trigger_statistics(episodes), status='computed',
                      reporting_only=True, used_for_decisions=False)
        # Fail inside the reporting boundary, before strict RunStore encoding.
        json.dumps(result, allow_nan=False)
        return result
    except Exception as exc:
        return dict(status='unavailable', error_type=type(exc).__name__,
                    reporting_only=True, used_for_decisions=False)
