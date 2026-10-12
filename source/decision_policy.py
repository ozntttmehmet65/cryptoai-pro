"""Small, predeclared validation-only search. Holdout is never an input.

Scores remain conditional on a resolved 24h bracket. Selection cannot grant
live permission: the frozen policy must subsequently pass the untouched test.
"""
from __future__ import annotations
import math
import numpy as np
from quality_guard import approved, tp_sl_direction_metrics

POLICY_VERSION = 'validation_direction_threshold_v2'
THRESHOLDS = (.55, .60, .65, .70, .75)
DEFAULT_THRESHOLD = .65
DIRECTION_MARGIN = .10


def valid_policy(policy):
    if not isinstance(policy, dict):
        return False
    return (policy.get('version') in (POLICY_VERSION, 'validation_threshold_v1')
            and policy.get('chosen_on') == 'chronological_validation_only'
            and policy.get('threshold') in THRESHOLDS
            and policy.get('direction_margin') == DIRECTION_MARGIN
            and policy.get('frozen_before_holdout') is True
            and (policy.get('version') != POLICY_VERSION or
                 isinstance(policy.get('thresholds'), dict) and
                 all(policy['thresholds'].get(s) in THRESHOLDS for s in ('LONG','SHORT')) and
                 policy['threshold']==min(policy['thresholds'][s] for s in ('LONG','SHORT'))))


def decide(p_long, p_short, policy=None):
    try:
        long, short = float(p_long), float(p_short)
    except (TypeError, ValueError):
        return 'BEKLE', 0.
    if not all(math.isfinite(p) and 0 <= p <= 1 for p in (long, short)):
        return 'BEKLE', 0.
    # Malformed new policy is not silently interpreted as legacy approval.
    if policy is not None and not valid_policy(policy):
        return 'BEKLE', 0.
    thresholds = policy['thresholds'] if policy and policy['version']==POLICY_VERSION else {}
    threshold = policy['threshold'] if policy else DEFAULT_THRESHOLD
    if long >= thresholds.get('LONG', threshold) and long >= short + DIRECTION_MARGIN:
        return 'LONG', long
    if short >= thresholds.get('SHORT', threshold) and short >= long + DIRECTION_MARGIN:
        return 'SHORT', short
    return 'BEKLE', 0.


def select_policy(p_long, p_short, outcomes, atrs, times, risk_plan, agreements=None):
    """All arrays MUST belong to validation, after training-label purge.

    Retain the original quality floors, stress assumption and 24h spacing.
    Prefer the larger threshold on tied scores. A failed search retains .65
    for observation and explicitly blocks new-model promotion.
    """
    summaries = []
    for threshold in THRESHOLDS:
        policy = dict(version=POLICY_VERSION, threshold=threshold,
                      thresholds={s:threshold for s in ('LONG','SHORT')},
                      direction_margin=DIRECTION_MARGIN,
                      chosen_on='chronological_validation_only',
                      frozen_before_holdout=True)
        actions = np.array([1 if side == 'LONG' else -1 if side == 'SHORT' else 0
                           for side, _ in (decide(l, s, policy) for l, s in zip(p_long, p_short))])
        if agreements is not None:
            for side, sign in (("LONG", 1), ("SHORT", -1)):
                actions[(actions == sign) & ~np.asarray(agreements[side], dtype=bool)] = 0
        directions = {}
        for side, sign, col in (('LONG', 1, 0), ('SHORT', -1, 1)):
            plan = risk_plan[side]
            metrics = tp_sl_direction_metrics(actions, outcomes[:, col], atrs, times, sign,
                                              plan['tp_atr'], plan['sl_atr'])
            positions = np.flatnonzero(actions == sign)
            resolved = [i for i in positions if outcomes[i, col] in (-1, 1)]
            nets = [(plan['tp_atr'] if outcomes[i, col] == 1 else -plan['sl_atr'])*float(atrs[i])-.004
                    for i in resolved]
            last = -10**30
            independent = 0
            for i in positions:
                if int(times[i])-last >= 86_400_000:
                    independent += 1
                    last = int(times[i])
            mean = float(np.mean(nets)) if nets else None
            passed = approved(metrics) and independent >= 3 and mean is not None and mean > 0
            directions[side] = dict(metrics, independent_signal_windows=independent,
                                    mean_stressed_net_return=mean, passed=bool(passed))
        passed = [m for m in directions.values() if m['passed']]
        summaries.append(dict(threshold=threshold, directions=directions,
                              passed_directions=len(passed),
                              objective=min(m['mean_stressed_net_return'] for m in passed) if passed else None))
    selected = {}
    for side in ('LONG','SHORT'):
        eligible = [row for row in summaries if row['directions'][side]['passed']]
        selected[side] = (max(eligible, key=lambda row:
                         (row['directions'][side]['mean_stressed_net_return'], row['threshold']))
                         if eligible else next(row for row in summaries if row['threshold']==DEFAULT_THRESHOLD))
    thresholds = {s:row['threshold'] for s,row in selected.items()}
    return dict(version=POLICY_VERSION, threshold=min(thresholds.values()), thresholds=thresholds,
                direction_margin=DIRECTION_MARGIN,
                chosen_on='chronological_validation_only', frozen_before_holdout=True,
                validation_passed={side:row['directions'][side]['passed'] for side,row in selected.items()},
                validation_summary={side:row['directions'][side] for side,row in selected.items()}, candidates=summaries,
                search_succeeded=any(row['directions'][side]['passed'] for side,row in selected.items()),
                holdout_used_for_selection=False)
