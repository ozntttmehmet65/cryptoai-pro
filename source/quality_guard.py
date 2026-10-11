"""Fail-closed directional validation and drift checks.

These checks do not promise profitability. Legacy model metadata is intentionally
unvalidated, rather than inferring permission from historical aggregate accuracy.
"""
from __future__ import annotations
import math

MIN_SIGNALS_PER_DIRECTION = 40
MIN_DISTINCT_TEST_CANDLES = 12
MIN_WIN_RATE = 0.56
MIN_MEAN_RETURN = 0.001  # after assumed costs; NOT real trading profit
MIN_WILSON_LOWER = 0.50


def wilson_lower(wins: int, trials: int, z: float = 1.96) -> float:
    if trials <= 0:
        return 0.0
    p = wins / trials
    return (p + z*z/(2*trials) - z * math.sqrt(p*(1-p)/trials + z*z/(4*trials*trials))) / (1+z*z/trials)


def direction_metrics(actions, true_returns, timestamps, direction: int) -> dict:
    idx = [i for i, action in enumerate(actions) if action == direction]
    n = len(idx)
    returns = [float(direction * true_returns[i] - 0.0016) for i in idx]
    wins = sum(direction * true_returns[i] > 0 for i in idx)
    return {
        "signals": n,
        "winning": wins,
        "hit_rate": round(wins / n, 5) if n else None,
        "wilson_lower_95": round(wilson_lower(wins, n), 5) if n else None,
        "mean_assumed_net_return": round(sum(returns) / n, 6) if n else None,
        "distinct_test_candles": len({int(timestamps[i]) for i in idx}),
    }


def tp_sl_direction_metrics(actions, outcomes, atr_percents, timestamps, direction: int,
                            tp_atr: float = 2.5, sl_atr: float = 1.5) -> dict:
    """Score first-touch TP/SL outcomes on unseen chronological candles.

    Unresolved within backtest LOOKAHEAD are neither invented SL nor TP.
    The validation must have sufficient *resolved* outcomes to approve a
    direction; success cannot be cherry-picked from only quick TP hits.
    """
    rows=[i for i,a in enumerate(actions) if a==direction]
    resolved=[i for i in rows if outcomes[i] in (1,-1)]
    tp_hits=int(sum(bool(outcomes[i]==1) for i in resolved))
    assumed_nets=[(tp_atr if outcomes[i]==1 else -sl_atr)*float(atr_percents[i])-0.0016
                  for i in resolved]
    n=len(rows)
    m=len(resolved)
    return {
        'signals':n,'resolved':m,'unresolved':n-m,
        'resolved_fraction':round(m/n,5) if n else None,
        'winning':tp_hits,
        'hit_rate':round(tp_hits/m,5) if m else None,
        'wilson_lower_95':round(wilson_lower(tp_hits,m),5) if m else None,
        'mean_assumed_net_return':round(sum(assumed_nets)/m,6) if m else None,
        'distinct_test_candles':len({int(timestamps[i]) for i in rows}),
    }


def approved(validation: dict | None) -> bool:
    """A training holdout is a prerequisite, not proof of future safety."""
    if not isinstance(validation, dict):
        return False
    return (validation.get("signals", 0) >= MIN_SIGNALS_PER_DIRECTION
            and validation.get("resolved", 0) >= MIN_SIGNALS_PER_DIRECTION
            and validation.get("resolved_fraction", 0) >= 0.85
            and validation.get("distinct_test_candles", 0) >= MIN_DISTINCT_TEST_CANDLES
            and validation.get("hit_rate") is not None
            and validation["hit_rate"] >= MIN_WIN_RATE
            and validation.get("wilson_lower_95") is not None
            and validation["wilson_lower_95"] >= MIN_WILSON_LOWER
            and validation.get("mean_assumed_net_return") is not None
            and validation["mean_assumed_net_return"] >= MIN_MEAN_RETURN)


def model_permissions(info: dict | None) -> dict:
    validation = (info or {}).get("directional_validation") or {}
    method = (info or {}).get('evaluation_method')
    method_ok = method in ('tp_sl_first_touch_24h_v1', 'tp_sl_first_touch_binary_ensemble_24h_v2', 'tp_sl_first_touch_binary_shrink_24h_v3', 'tp_sl_first_touch_pattern_mtf_24h_v4', 'tp_sl_first_touch_adaptive_bracket_24h_v5')
    result = {}
    for direction in ("LONG", "SHORT"):
        d = validation.get(direction)
        ok = method_ok and approved(d)
        if method in ('tp_sl_first_touch_binary_ensemble_24h_v2', 'tp_sl_first_touch_binary_shrink_24h_v3', 'tp_sl_first_touch_pattern_mtf_24h_v4', 'tp_sl_first_touch_adaptive_bracket_24h_v5'):
            ok = bool(ok and (info or {}).get('model_family') == ('tp-sl-adaptive-brackets-per-symbol-v60' if method.endswith('_v5') else 'tp-sl-pattern-mtf-per-symbol-v59' if method.endswith('_v4') else 'tp-sl-ensemble-shrunk-per-symbol-v58' if method.endswith('_v3') else 'tp-sl-ensemble-per-symbol-v57')
                      and d.get('validation_outperformed_baseline') is True
                      and d.get('holdout_outperformed_baseline') is True
                      and d.get('independent_signal_windows',0) >= 3
                      and d.get('mean_stressed_net_return') is not None
                      and d['mean_stressed_net_return'] > 0)
            if method.endswith(('_v3','_v4','_v5')):
                # Validation-only calibration must retain nontrivial learned
                # signal; a constant past base rate is never an AI edge.
                ok = bool(ok and d.get('calibration_alpha',0) >= .25
                          and d.get('calibration_sample_count',0) >= 35)
        if method == 'tp_sl_first_touch_adaptive_bracket_24h_v5':
            plan = ((info or {}).get('risk_plan') or {}).get(direction) or {}
            tp,sl=plan.get('tp_atr'),plan.get('sl_atr')
            try:
                ok = bool(ok and 0 < float(tp) <= 6 and 0 < float(sl) <= 6
                    and plan.get('chosen_on') in ('training_only_stressed_net','predeclared_fallback'))
            except (ValueError,TypeError):
                ok = False
        if (info or {}).get('ensemble_policy'):
            ok = bool(ok and info['ensemble_policy'] == 'weighted_soft_vote_consensus_v71'
                      and (d or {}).get('ensemble_policy') == info['ensemble_policy']
                      and (d or {}).get('ensemble_member_count',0) >= 2)
        if (info or {}).get('professional_policy'):
            audit=(d or {}).get('professional_audit') or {}
            ok=bool(ok and info['professional_policy']=='professional_v72'
                and audit.get('policy')==info['professional_policy'] and audit.get('passed') is True)
        result[direction] = {
            "approved": ok,
            "reason": ("Kronolojik test doğrulaması mevcut" if ok else
                       "TP/SL ilk temasına dayalı bağımsız yön testi yetersiz veya eski yöntem; sinyal kapalı"),
            "validation": d,
        }
    return result


def live_drift_block(db, direction: str, now_ms: int, symbol=None, model_version=None) -> tuple[bool, dict]:
    """Block a previously approved direction following poor *new-method* live paper results."""
    query = """SELECT net_return FROM paper_observations
           WHERE state='CLOSED' AND settlement_method='tp_sl_live_quote_v1'
             AND signal=? AND result_at>=?"""
    params=[direction,now_ms-6*3600*1000]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='executable_paper'").fetchone():
        query="SELECT net_return FROM executable_paper WHERE state='CLOSED' AND shadow=0 AND signal=? AND result_at>=?"
    if symbol is not None:
        query+=' AND symbol=?';params.append(symbol)
    if model_version is not None:
        query+=' AND model_version=?';params.append(model_version)
    rows=db.execute(query+' ORDER BY result_at DESC LIMIT 50',params).fetchall()
    returns = [float(row[0]) for row in rows if row[0] is not None and math.isfinite(float(row[0]))]
    n = len(returns)
    recent = returns[:20]
    hits = sum(v > 0 for v in recent)
    mean = sum(recent) / len(recent) if recent else None
    # Avoid reacting to tiny samples; stricter safety trigger when clear deterioration.
    blocked = len(recent) >= 15 and (hits / len(recent) < 0.45 or mean < 0)
    return blocked, {"samples": n, "last_n": len(recent),
                     "last_n_win_rate": round(hits / len(recent), 4) if recent else None,
                     "last_n_mean_net": round(mean, 6) if mean is not None else None,
                     "blocked": blocked}
