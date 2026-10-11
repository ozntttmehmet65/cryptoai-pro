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


def _floor(value, minimum, strict=False):
    try:
        n = float(value)
        return math.isfinite(n) and (n > minimum if strict else n >= minimum)
    except (TypeError, ValueError, OverflowError):
        return False


def validation_checks(validation):
    d = validation if isinstance(validation, dict) else {}
    limits = (
        ('signals', 'Test aday sayısı', MIN_SIGNALS_PER_DIRECTION),
        ('resolved', 'Sonuçlanan TP/SL sayısı', MIN_SIGNALS_PER_DIRECTION),
        ('resolved_fraction', 'Sonuçlanma oranı', .85),
        ('distinct_test_candles', 'Farklı test mumları', MIN_DISTINCT_TEST_CANDLES),
        ('hit_rate', 'TP ilk temas oranı', MIN_WIN_RATE),
        ('wilson_lower_95', 'Wilson alt sınırı', MIN_WILSON_LOWER),
        ('mean_assumed_net_return', 'Masraf sonrası ortalama', MIN_MEAN_RETURN),
    )
    return [dict(id=key, label=label, actual=d.get(key), required=minimum,
                 passed=_floor(d.get(key), minimum)) for key, label, minimum in limits]


def approved(validation: dict | None) -> bool:
    """A training holdout is a prerequisite, not proof of future safety."""
    return all(row['passed'] for row in validation_checks(validation))


def model_permissions(info: dict | None) -> dict:
    info = info or {}
    validation = info.get('directional_validation') or {}
    method = info.get('evaluation_method')
    families = {
        'tp_sl_first_touch_binary_ensemble_24h_v2': 'tp-sl-ensemble-per-symbol-v57',
        'tp_sl_first_touch_binary_shrink_24h_v3': 'tp-sl-ensemble-shrunk-per-symbol-v58',
        'tp_sl_first_touch_pattern_mtf_24h_v4': 'tp-sl-pattern-mtf-per-symbol-v59',
        'tp_sl_first_touch_adaptive_bracket_24h_v5': 'tp-sl-adaptive-brackets-per-symbol-v60',
    }
    result = {}
    for direction in ('LONG', 'SHORT'):
        d = validation.get(direction) or {}
        checks = validation_checks(d)
        def check(key, label, actual, required, passed):
            checks.append(dict(id=key, label=label, actual=actual, required=required, passed=bool(passed)))
        check('evaluation_method', 'TP/SL test yöntemi', method, 'supported_first_touch',
              method == 'tp_sl_first_touch_24h_v1' or method in families)
        if method in families:
            check('model_family', 'Model ailesi', info.get('model_family'), families[method], info.get('model_family') == families[method])
            for key, label in (('validation_outperformed_baseline', 'Doğrulamada baz model üstünlüğü'),
                               ('holdout_outperformed_baseline', 'Ayrı testte baz model üstünlüğü')):
                check(key, label, d.get(key), True, d.get(key) is True)
            check('independent_signal_windows', 'Bağımsız 24 saat pencereleri', d.get('independent_signal_windows'), 3, _floor(d.get('independent_signal_windows'), 3))
            check('mean_stressed_net_return', 'Stres maliyeti sonrası ortalama', d.get('mean_stressed_net_return'), '>0', _floor(d.get('mean_stressed_net_return'), 0, strict=True))
            if method.endswith(('_v3', '_v4', '_v5')):
                check('calibration_alpha', 'Kalibrasyon öğrenilmiş sinyal payı', d.get('calibration_alpha'), .25, _floor(d.get('calibration_alpha'), .25))
                check('calibration_sample_count', 'Kalibrasyon örnekleri', d.get('calibration_sample_count'), 35, _floor(d.get('calibration_sample_count'), 35))
        if method == 'tp_sl_first_touch_adaptive_bracket_24h_v5':
            plan = (info.get('risk_plan') or {}).get(direction) or {}
            bracket_ok = all(_floor(plan.get(k), 0, strict=True) and _floor(6, plan.get(k)) for k in ('tp_atr', 'sl_atr'))
            check('risk_plan', 'Eğitimde dondurulan TP/SL', plan.get('chosen_on'), 'training_only_or_predeclared',
                  bracket_ok and plan.get('chosen_on') in ('training_only_stressed_net', 'predeclared_fallback'))
        if info.get('artifact_schema', 0) >= 3 or 'decision_policy' in info:
            from decision_policy import valid_policy
            policy = info.get('decision_policy')
            check('decision_policy', 'Doğrulamada dondurulan aday eşiği', policy.get('threshold') if isinstance(policy, dict) else None,
                  'validation_only_frozen', valid_policy(policy))
            check('validation_policy_passed', 'Seçilen eşik doğrulama testi',
                  (policy.get('validation_passed') or {}).get(direction) if isinstance(policy, dict) else None,
                  True, isinstance(policy, dict) and (policy.get('validation_passed') or {}).get(direction) is True)
        if info.get('ensemble_policy'):
            check('ensemble_policy', 'Çoklu model politikası', info.get('ensemble_policy'), 'weighted_soft_vote_consensus_v71',
                  info.get('ensemble_policy') == 'weighted_soft_vote_consensus_v71' and d.get('ensemble_policy') == info.get('ensemble_policy'))
            check('ensemble_member_count', 'Oylayan model sayısı', d.get('ensemble_member_count'), 2, _floor(d.get('ensemble_member_count'), 2))
        if info.get('professional_policy'):
            audit = d.get('professional_audit') or {}
            check('professional_policy', 'Profesyonel kanıt politikası', audit.get('policy'), 'professional_v72',
                  info.get('professional_policy') == 'professional_v72' and audit.get('policy') == info.get('professional_policy'))
            for key, label in (('history', 'Profesyonel geçmiş uzunluğu'), ('independent_events', 'Profesyonel bağımsız olay sayısı'),
                               ('resolution', 'Profesyonel sonuçlanma oranı'), ('family_screen', 'Çoklu model seçim düzeltmesi'),
                               ('stressed_lower_bound', 'Stres başabaşının üstünde alt sınır'), ('stressed_mean', 'Profesyonel stres sonrası ortalama'),
                               ('period_consistency', 'Üç kronolojik dönemin tutarlılığı')):
                check('professional_'+key, label, (audit.get('checks') or {}).get(key), True, (audit.get('checks') or {}).get(key) is True)
            check('professional_passed', 'Profesyonel test sonucu', audit.get('passed'), True, audit.get('passed') is True)
        failed = [row for row in checks if not row['passed']]
        ok = not failed
        result[direction] = dict(approved=ok,
            reason='Kronolojik test doğrulaması mevcut' if ok else '; '.join(row['label'] for row in failed),
            validation=d or None, checks=checks, failed_checks=[row['id'] for row in failed])
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
