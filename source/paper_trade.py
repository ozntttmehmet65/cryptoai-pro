"""Forward-only hypothetical TP/SL tracking, without time-forced exits or real orders.

The entry is the first actually observed live ticker when the model signal arrives.
Its bracket is fixed using the model's last closed-candle ATR *percentage*,
rebased to the actually observed entry quote. A new signal does not move the
bracket or close an existing hypothetical position. Only a NEW Binance quote
whose event timestamp is after the entry may mark a barrier reached.

Caveat: the exchange !ticker stream consists of sampled last-trade prices, not
executable bid/ask or a tick-complete trade tape. Touches between updates can be
missed. Fees, funding, slippage, liquidation, spread and exchange outages are
not actual execution data. Old bar-close/45m observations are EXCLUDED from
these statistics, not retroactively rewritten.
"""
from __future__ import annotations
from datetime import datetime, timezone
import math
import sqlite3
from quality_guard import wilson_lower

ROUND_TRIP_ASSUMED_COST = 0.0016
METHOD = 'tp_sl_live_quote_v1'
SHADOW_METHOD = 'tp_sl_shadow_quote_v1'
LEGACY = 'legacy_close_bar_60m'
PREVIOUS_METHODS = (LEGACY, 'live_quote_45m', 'shadow_quote_45m')
QUOTE_MAX_AGE_MS = 25_000
# A discontinuity in the stream can invalidate which barrier was first touched.
# Do not force-close after any predetermined investment holding interval.
MAX_UNVERIFIED_GAP_MS = 180_000


def setup(db: sqlite3.Connection):
    db.execute('''CREATE TABLE IF NOT EXISTS paper_observations (
        symbol TEXT NOT NULL, candle_time INTEGER NOT NULL, model_version TEXT NOT NULL,
        signal TEXT NOT NULL, confidence REAL NOT NULL, observed_at INTEGER NOT NULL,
        entry_price REAL NOT NULL, result_at INTEGER, result_price REAL,
        net_return REAL, state TEXT NOT NULL DEFAULT 'OPEN',
        settlement_method TEXT NOT NULL DEFAULT 'legacy_close_bar_60m',
        stop_price REAL, target_price REAL, exit_reason TEXT, last_quote_ms INTEGER,
        PRIMARY KEY (symbol, candle_time, model_version))''')
    cols = {r[1] for r in db.execute('PRAGMA table_info(paper_observations)')}
    extra = {'settlement_method': "TEXT NOT NULL DEFAULT 'legacy_close_bar_60m'",
             'stop_price': 'REAL', 'target_price': 'REAL',
             'exit_reason': 'TEXT', 'last_quote_ms': 'INTEGER'}
    for col, definition in extra.items():
        if col not in cols:
            db.execute(f'ALTER TABLE paper_observations ADD COLUMN {col} {definition}')
    db.execute('CREATE INDEX IF NOT EXISTS paper_symbol_open ON paper_observations(symbol,state)')
    db.execute('CREATE INDEX IF NOT EXISTS paper_evaluation_method ON paper_observations(settlement_method,state,signal,result_at)')
    db.commit()


def _bracket(side: str, entry: float, atr_pct: float, tp_atr: float = 2.5, sl_atr: float = 1.5):
    """Use *real quote entry*, not the previous candle's close, for fixed ATR bracket."""
    if side not in ('LONG','SHORT') or not all(map(math.isfinite,(entry,atr_pct))):
        return None
    if entry <= 0 or not 0 < atr_pct < 0.30 or not 0 < tp_atr <= 6 or not 0 < sl_atr <= 6:
        return None
    stop_offset = entry * atr_pct * sl_atr
    tp_offset = entry * atr_pct * tp_atr
    stop = entry - stop_offset if side=='LONG' else entry + stop_offset
    target = entry + tp_offset if side=='LONG' else entry - tp_offset
    if min(stop,target) <= 0 or not all(map(math.isfinite,(stop,target))):
        return None
    return stop,target


def observe(db, symbol: str, signal: dict, observed_price: float | None,
            observed_ms: int, *, shadow: bool=False):
    side=signal.get('signal')
    if side not in ('LONG','SHORT'):
        return False
    try:
        entry=float(observed_price)
        atr_pct=float(signal.get('risk_atr_pct'))
        candle_time=int(signal['candle_time'])
        confidence=float(signal['confidence'])
        model_version=str(signal['model_version'])
    except (TypeError,ValueError,OverflowError,KeyError):
        return False
    # Keep new strategy brackets identical to the historical model labels.
    # Old observations retain their historical 2.5/1.5 ATR geometry.
    try:
        tp_atr=float(signal.get('tp_atr',2.5))
        sl_atr=float(signal.get('sl_atr',1.5))
    except (ValueError,TypeError,OverflowError):
        return False
    bracket=_bracket(side,entry,atr_pct,tp_atr=tp_atr,sl_atr=sl_atr)
    if bracket is None or not math.isfinite(confidence) or not model_version:
        return False
    # Reject hindsight observations or observations very late after candle close.
    if not 0 <= observed_ms - (candle_time + 900000) <= 15*60*1000:
        return False
    # Legacy 45/60-minute OPEN rows cannot be reconciled by this method.
    # Do not let such old records permanently block a new TP/SL observation.
    if db.execute("""SELECT 1 FROM paper_observations
                     WHERE symbol=? AND state='OPEN'
                       AND settlement_method IN (?,?) LIMIT 1""",
                  (symbol, METHOD, SHADOW_METHOD)).fetchone():
        return False
    stop,target=bracket
    before=db.total_changes
    db.execute('''INSERT OR IGNORE INTO paper_observations
        (symbol,candle_time,model_version,signal,confidence,observed_at,
         entry_price,settlement_method,stop_price,target_price,last_quote_ms)
         VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
       (symbol,candle_time,model_version,side,confidence,observed_ms,
        entry,SHADOW_METHOD if shadow else METHOD,stop,target,observed_ms))
    db.commit()
    return db.total_changes>before


def settle(db, symbol: str, candles: list[dict], now_ms: int):
    """Historical OHLC cannot identify a first TP-vs-SL touch within a candle."""
    return 0


def reconcile_quotes(db, quotes: dict, now_ms: int):
    """Mark the FIRST sampled Binance ticker TP/SL touch; never time-close.

    If quote continuity is lost, mark the hypothetical track unverified instead
    of falsely choosing a TP or SL after a gap. Even at steady 1s sampling,
    results remain *indicative*, not exchange order executions.
    """
    open_rows=db.execute('''SELECT symbol,candle_time,model_version,signal,observed_at,
                                  entry_price,stop_price,target_price,last_quote_ms
                            FROM paper_observations
                            WHERE state='OPEN' AND settlement_method IN (?,?)''',
                         (METHOD,SHADOW_METHOD)).fetchall()
    changed=0
    for symbol,candle_time,model_version,side,observed_at,entry,stop,tp,last_seen in open_rows:
        q=quotes.get(symbol)
        if not q:
            continue  # Inactive coin may simply have no trades for a while.
        try:
            price=float(q.get('price'))
            event_ms=round(float(q.get('updated_at'))*1000)
        except (TypeError,ValueError,OverflowError):
            continue
        if not math.isfinite(price) or price<=0 or event_ms<observed_at or event_ms<=int(last_seen or observed_at):
            continue
        if event_ms>now_ms+2000 or now_ms-event_ms>QUOTE_MAX_AGE_MS:
            continue
        if event_ms-int(last_seen or observed_at)>MAX_UNVERIFIED_GAP_MS:
            db.execute('''UPDATE paper_observations
                          SET state='VERI_EKSIK',exit_reason='QUOTE_GAP',last_quote_ms=?
                          WHERE symbol=? AND candle_time=? AND model_version=? AND state='OPEN' ''',
                       (event_ms,symbol,candle_time,model_version))
            changed+=1
            continue
        tp_hit=price>=tp if side=='LONG' else price<=tp
        sl_hit=price<=stop if side=='LONG' else price>=stop
        if tp_hit or sl_hit:
            reason='TP' if tp_hit else 'SL'
            # A favourable jump is not credited beyond a resting limit target;
            # adverse stop gaps retain the sampled adverse price.
            result=tp if tp_hit else price
            net=(1 if side=='LONG' else -1)*(result/entry-1)-ROUND_TRIP_ASSUMED_COST
            db.execute('''UPDATE paper_observations
                          SET result_at=?,result_price=?,net_return=?,state='CLOSED',
                              exit_reason=?,last_quote_ms=?
                          WHERE symbol=? AND candle_time=? AND model_version=? AND state='OPEN' ''',
                       (event_ms,result,net,reason,event_ms,symbol,candle_time,model_version))
            changed+=1
        else:
            db.execute('''UPDATE paper_observations SET last_quote_ms=?
                          WHERE symbol=? AND candle_time=? AND model_version=? AND state='OPEN' ''',
                       (event_ms,symbol,candle_time,model_version))
    # We intentionally do NOT close or assign a profit/loss when 45m, 10h, or
    # any arbitrary holding period has elapsed.
    # Persist last seen event time for restart integrity, not merely closed trades.
    db.commit()
    return changed



def directional_report(db):
    """All approved forward observations, grouped by side; no shadow/legacy rows."""
    result = {}
    for side in ('LONG', 'SHORT'):
        row = db.execute("""SELECT COUNT(*),
            SUM(CASE WHEN state='CLOSED' THEN 1 ELSE 0 END),
            SUM(CASE WHEN state='CLOSED' AND exit_reason='TP' THEN 1 ELSE 0 END),
            SUM(CASE WHEN state='CLOSED' AND net_return>0 THEN 1 ELSE 0 END),
            AVG(CASE WHEN state='CLOSED' THEN net_return END),
            SUM(CASE WHEN state='CLOSED' AND net_return>0 THEN net_return ELSE 0 END),
            SUM(CASE WHEN state='CLOSED' AND net_return<0 THEN -net_return ELSE 0 END),
            SUM(CASE WHEN state='VERI_EKSIK' THEN 1 ELSE 0 END)
            FROM paper_observations WHERE settlement_method=? AND signal=?""", (METHOD, side)).fetchone()
        total, closed, tp, wins, mean, gain, loss, gaps = row
        closed, tp, wins = int(closed or 0), int(tp or 0), int(wins or 0)
        result[side] = {'observed':total, 'settled':closed, 'unverified':int(gaps or 0),
            'resolved_fraction':closed/total if total else None,
            'tp_first_rate':tp/closed if closed else None,
            'net_positive_rate':wins/closed if closed else None,
            'tp_wilson_lower_95':wilson_lower(tp, closed) if closed else None,
            'mean_net_return':mean,
            'profit_factor':gain/loss if loss else None,
            'profit_factor_status':'measured' if loss else 'no_losses' if gain else 'no_results',
            'evidence_status':'YETERLİ ÖRNEK' if closed >= 40 else 'ÖRNEK AZ',
            'notice':'Sanal gözlemdir; çözülmeyen ve veri kesintili kayıtlar TP sayılmaz'}
    return result


def report(db,limit=100):
    rows=db.execute('''SELECT symbol,signal,confidence,observed_at,entry_price,result_at,
                              result_price,net_return,state,settlement_method,
                              stop_price,target_price,exit_reason,last_quote_ms
                       FROM paper_observations ORDER BY observed_at DESC LIMIT ?''',(limit,)).fetchall()
    stats=dict(db.execute('''SELECT state,COUNT(*) FROM paper_observations
                             WHERE settlement_method=? GROUP BY state''',(METHOD,)).fetchall())
    total=sum(stats.values())
    closed=int(stats.get('CLOSED',0))
    tp_count=db.execute('''SELECT COUNT(*) FROM paper_observations
                           WHERE settlement_method=? AND state='CLOSED' AND exit_reason='TP' ''',(METHOD,)).fetchone()[0]
    sl_count=db.execute('''SELECT COUNT(*) FROM paper_observations
                           WHERE settlement_method=? AND state='CLOSED' AND exit_reason='SL' ''',(METHOD,)).fetchone()[0]
    winning=db.execute('''SELECT COUNT(*) FROM paper_observations
                          WHERE settlement_method=? AND state='CLOSED' AND net_return>0 ''',(METHOD,)).fetchone()[0]
    mean=db.execute('''SELECT AVG(net_return) FROM paper_observations
                       WHERE settlement_method=? AND state='CLOSED' ''',(METHOD,)).fetchone()[0]
    legacy_count=db.execute("SELECT COUNT(*) FROM paper_observations WHERE settlement_method NOT IN (?,?)",(METHOD,SHADOW_METHOD)).fetchone()[0]
    shadow_all=db.execute('''SELECT COUNT(*) FROM paper_observations WHERE settlement_method=?''',(SHADOW_METHOD,)).fetchone()[0]
    shadow_closed=db.execute('''SELECT COUNT(*) FROM paper_observations
                               WHERE settlement_method=? AND state='CLOSED' ''',(SHADOW_METHOD,)).fetchone()[0]
    shadow_tp=db.execute('''SELECT COUNT(*) FROM paper_observations
                           WHERE settlement_method=? AND state='CLOSED' AND exit_reason='TP' ''',(SHADOW_METHOD,)).fetchone()[0]
    shadow_mean=db.execute('''SELECT AVG(net_return) FROM paper_observations
                            WHERE settlement_method=? AND state='CLOSED' ''',(SHADOW_METHOD,)).fetchone()[0]
    shadow_sl=db.execute('''SELECT COUNT(*) FROM paper_observations
                           WHERE settlement_method=? AND state='CLOSED' AND exit_reason='SL' ''',(SHADOW_METHOD,)).fetchone()[0]
    shadow_unverified=db.execute('''SELECT COUNT(*) FROM paper_observations
                           WHERE settlement_method=? AND state='VERI_EKSIK' ''',(SHADOW_METHOD,)).fetchone()[0]
    return {'directional':directional_report(db), 'summary':{'observed':total,'settled':closed,'winning':winning,
                       'tp_count':tp_count,'sl_count':sl_count,
                       'win_rate':winning/closed if closed else None,
                       'tp_first_rate':tp_count/(tp_count+sl_count) if tp_count+sl_count else None,
                       'mean_net_return':mean,'by_state':stats,
                       'legacy_observations_excluded':legacy_count,
                       'shadow_observations':shadow_all,'shadow_settled':shadow_closed,
                       'shadow_tp_count':shadow_tp,'shadow_sl_count':shadow_sl,
                       'shadow_unverified':shadow_unverified,
                       'shadow_tp_first_rate':shadow_tp/(shadow_tp+shadow_sl) if shadow_tp+shadow_sl else None,
                       'shadow_mean_net_return':shadow_mean},
            'assumed_cost':ROUND_TRIP_ASSUMED_COST,
            'method':METHOD,
            'disclaimer':('TP/SL yalnızca gözlemlenmiş Binance son işlem fiyatından takip edilir; '
                '45 dakika, 100 saat veya herhangi bir süre dolduğu için kapatılmaz. Canlı akış koparsa ilk temas '
                'doğrulanamaz ve veri eksik olarak işaretlenir. Eski 45/60dk sonuçları yeni '
                'istatistiklerden hariçtir. Gerçek emir, spread, funding, tasfiye ve değişken '
                'kayma hesaba katılmaz; simülasyon gerçek kazanç kanıtı değildir.'),
            'rows':[{'symbol':r[0],'signal':r[1],'confidence':r[2],
                     'observed_at':datetime.fromtimestamp(r[3]/1000,tz=timezone.utc).isoformat(),
                     'entry_price':r[4],
                     'result_at':datetime.fromtimestamp(r[5]/1000,tz=timezone.utc).isoformat() if r[5] else None,
                     'result_price':r[6],'net_return':r[7], 'state':r[8],
                     'settlement_method':r[9],'stop_price':r[10],
                     'target_price':r[11],'exit_reason':r[12],
                     'last_quote_at':datetime.fromtimestamp(r[13]/1000,tz=timezone.utc).isoformat() if r[13] else None,
                     'legacy':r[9] not in (METHOD,SHADOW_METHOD),
                     'shadow':r[9]==SHADOW_METHOD} for r in rows]}
