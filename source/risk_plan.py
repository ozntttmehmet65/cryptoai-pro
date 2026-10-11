"""V6: frozen, per-coin/per-direction ATR bracket research (NO holdout tuning).

Select a small predeclared TP/SL menu exclusively on TRAIN timestamps, purge
all forward label windows from the validation and holdout periods. Store the
frozen choice, use identical geometry for labels, predictions and paper trades.
No parameter choice uses chronological validation/holdout outcomes.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from ml_engine import INTERVAL_MS, EVALUATION_BARS
from quality_guard import wilson_lower
from technical_analysis import setup_arrays

# Multipliers (TP ATR, SL ATR), selected once per symbol and direction.
BRACKET_GRID = ((2.5, 1.5), (1.75, 1.25), (2., 1.5), (3., 1.75))
FALLBACK = (2.5, 1.5)
ASSUMED_STRESS_COST = 0.004


def all_bracket_outcomes(candles, tp_atr: float, sl_atr: float, frame: pd.DataFrame):
    """Align causal future first-touch labels to *frame* candle times.

    Unresolved=0, ambiguous same-15m-bar touch=2, TP-first=1, SL-first=-1.
    Missing bars or unavailable full 24h future => censored=0. No time-based
    live exit is implied; 24h applies to historical supervised labels only.
    """
    if not (0 < sl_atr <= 6 and 0 < tp_atr <= 6):
        raise ValueError('Invalid bracket')
    raw = pd.DataFrame(candles).sort_values('time').drop_duplicates('time')
    t = raw['time'].to_numpy(dtype=np.int64)
    n = len(raw)
    result = np.zeros((len(frame), 2), dtype=np.int8)
    if n <= EVALUATION_BARS:
        return result
    close = raw['close'].to_numpy(dtype=float)
    highs = raw['high'].to_numpy(dtype=float)
    lows = raw['low'].to_numpy(dtype=float)
    atrmap = pd.Series(frame['atr_pct'].to_numpy(dtype=float), index=frame['time'].to_numpy(dtype=np.int64))
    times = frame['time'].to_numpy(dtype=np.int64)
    loc = np.searchsorted(t, times)
    valid_pos = (loc < n)
    loc_safe = np.minimum(loc, n-1)
    valid_pos &= t[loc_safe] == times
    valid_pos &= loc + EVALUATION_BARS < n
    gaps = np.r_[0, np.cumsum(np.diff(t) != INTERVAL_MS)]
    valid_pos &= gaps[np.minimum(loc+EVALUATION_BARS,n-1)] == gaps[loc_safe]
    try:
        atrp = atrmap.reindex(times).to_numpy(dtype=float)
    except (TypeError, ValueError):
        atrp = frame['atr_pct'].to_numpy(dtype=float)
    valid_pos &= np.isfinite(atrp) & (atrp > 0) & (close[loc_safe] > 0)
    # SHORT target or LONG stop below zero is not an executable bracket.
    valid_pos &= atrp*max(tp_atr, sl_atr) < 1.
    if not valid_pos.any():
        return result
    rows = np.flatnonzero(valid_pos)
    ix = loc[rows]
    p = close[ix]
    atr = atrp[rows]
    # Future OHLC starts at the NEXT 15m candle; never use entry candle high/low.
    future_hi = sliding_window_view(highs[1:], EVALUATION_BARS)[ix]
    future_lo = sliding_window_view(lows[1:], EVALUATION_BARS)[ix]
    for j, sign in enumerate((1, -1)):
        tp = p * (1 + sign*tp_atr*atr)
        sl = p * (1 - sign*sl_atr*atr)
        if sign == 1:
            hit_tp = future_hi >= tp[:, None]
            hit_sl = future_lo <= sl[:, None]
        else:
            hit_tp = future_lo <= tp[:, None]
            hit_sl = future_hi >= sl[:, None]
        t_any = hit_tp.any(axis=1)
        s_any = hit_sl.any(axis=1)
        t_idx = np.argmax(hit_tp, axis=1)
        s_idx = np.argmax(hit_sl, axis=1)
        out = np.zeros(len(rows), dtype=np.int8)
        out[s_any & (~t_any | (s_idx < t_idx))] = -1
        out[t_any & (~s_any | (t_idx < s_idx))] = 1
        out[t_any & s_any & (t_idx == s_idx)] = 2
        result[rows, j] = out
    return result


def _independent(stamps):
    last = -10**30
    count = 0
    for x in sorted(set(int(x) for x in stamps)):
        if x - last >= EVALUATION_BARS * INTERVAL_MS:
            count += 1
            last = x
    return count


def choose_on_train(candles, frame, train_cutoff: int):
    """Select only from TRAIN, based on fixed multi-timeframe entry setups.

    No validation/holdout prices or outcomes influence parameter selection.
    Baseline is retained unless a candidate improves a penalized, stressed
    training objective with at least four independent opportunity windows.
    """
    train_mask = frame['time'].to_numpy(dtype=np.int64) < int(train_cutoff)
    idx = np.flatnonzero(train_mask)
    if len(idx) < 240:
        raise ValueError('Not enough independent training candles')
    long_score, short_score = setup_arrays(frame)
    times = frame['time'].to_numpy(dtype=np.int64)
    selected = {}
    label_cache = {}
    for tp,sl in BRACKET_GRID:
        label_cache[(tp,sl)] = all_bracket_outcomes(candles,tp,sl,frame)
    for col, side, score, opposite in (
        (0, 'LONG', long_score, short_score),
        (1, 'SHORT', short_score, long_score)
    ):
        # Entry timing: independent 1h/4h trend and pattern/momentum agreement,
        # frozen at decision time; avoid optimized future-dependent filters.
        eligible = train_mask & (score >= 6.5) & (score >= opposite+1.5)
        candidates=[]
        for tp,sl in BRACKET_GRID:
            outcomes = label_cache[(tp,sl)][:,col]
            trade_indices = np.flatnonzero(eligible)
            resolved = trade_indices[np.isin(outcomes[trade_indices], (-1,1))]
            if not len(resolved):
                continue
            independent = _independent(times[resolved])
            if len(resolved) < 30 or independent < 4 or len(resolved) / max(len(trade_indices),1) < .75:
                continue
            wins = int((outcomes[resolved]==1).sum())
            ap = frame['atr_pct'].to_numpy(dtype=float)[resolved]
            net = np.where(outcomes[resolved]==1,tp,-sl)*ap - ASSUMED_STRESS_COST
            mean = float(np.mean(net))
            # Penalize uncertain win rates and menu selection to avoid picking
            # an unusually lucky bracket after looking at 4 configurations.
            uncertainty = 0.002 / np.sqrt(independent)
            objective = mean-uncertainty
            candidates.append({'tp_atr':tp,'sl_atr':sl,'signals':int(len(trade_indices)),
                               'resolved':int(len(resolved)), 'tp_first':wins,
                               'tp_rate':round(wins/len(resolved),5),
                               'wilson_95':round(wilson_lower(wins,len(resolved)),5),
                               'independent_windows':independent,
                               'mean_stressed_net':round(mean,6),
                               'selection_objective':round(objective,6)})
        # Fallback never pretends its bracket was empirically selected.
        default={'tp_atr':FALLBACK[0],'sl_atr':FALLBACK[1],
                 'chosen_on':'predeclared_fallback','train_candidate_count':len(candidates),
                 'entry_mode':'causal_1h4h_trend_technical_score','candidate_summary':candidates}
        if candidates:
            baseline = next((v for v in candidates if (v['tp_atr'],v['sl_atr'])==FALLBACK),None)
            champion=max(candidates,key=lambda v:v['selection_objective'])
            # Require cost-adjusted training improvement against frozen default
            # of at least 0.10pp. Never automatically choose negative expected net.
            if (champion['mean_stressed_net'] > 0.0 and
                (baseline is None or champion['selection_objective'] >= baseline['selection_objective']+.001)):
                default.update({'tp_atr':champion['tp_atr'],'sl_atr':champion['sl_atr'],
                                'chosen_on':'training_only_stressed_net','training_stats':champion})
        selected[side]=default
    return selected,label_cache


def entry_guidance(last, direction: str):
    """Causal, explanatory timing only; never falsely promise a filled price."""
    if direction not in ('LONG','SHORT'):
        return {'status':'BEKLE','why':'Yeterli yön teyidi yok'}
    sign = 1 if direction=='LONG' else -1
    trend = sign*float(last.get('h1_trend',0)) > 0 and sign*float(last.get('h4_trend',0)) > 0
    breakout = float(last.get('breakout_bull' if sign==1 else 'breakout_bear',0)) > .5
    oversold = float(last.get('bb_position',0))*sign <= -.7
    if not trend:
        return {'status':'TREND TEYİDİ BEKLE','why':'1 saat ve 4 saat aynı yönü desteklemiyor'}
    if breakout:
        return {'status':'KIRILIM TEYİTLİ','why':'Kapanmış mumda hacimli kırılım var; fiyatın takip edilmesi gerekir'}
    if oversold:
        return {'status':'GERİ ÇEKİLME TAKİP','why':'Üst zaman dilimi trendinde alt bant bölgesi'}
    return {'status':'MUM KAPANIŞINI TAKİP','why':'Trend teyidi var; teyitli giriş formasyonu beklenebilir'}
