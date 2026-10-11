"""Read-only, candle-close-based 15m futures ingestion from the Binance Market WebSocket.

No exchange orders, no synthetic bars, no IP-ban circumvention. Persisted closed bars
provide auditable coverage; missing intervals never get filled with fake prices.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import random
import sqlite3
import time
import websockets
from urllib.parse import quote

LOG = logging.getLogger(__name__)
INTERVAL_MS = 900_000
WS_ENDPOINT = "wss://fstream.binance.com/market/stream"
# Multi-socket public market streams: no orders and no API key. The cap is a
# resource safety valve; the complete eligible USD-M universe is followed up to
# this capacity. Split streams to keep individual URL lengths and memory bounded.
import os
MAX_WATCHED = max(24, min(int(os.environ.get("MAX_WATCHED", "700")), 900))
STREAMS_PER_SOCKET = max(20, min(int(os.environ.get("KLINE_SOCKET_SIZE", "72")), 90))
PREFERRED = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")


def setup(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS completed_klines (
        symbol TEXT NOT NULL, open_time INTEGER NOT NULL,
        open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
        close REAL NOT NULL, volume REAL NOT NULL, close_time INTEGER NOT NULL,
        PRIMARY KEY (symbol, open_time))""")
    db.execute("CREATE INDEX IF NOT EXISTS completed_klines_symbol ON completed_klines(symbol,open_time)")
    db.commit()


def selected_symbols(universe, quotes, count=MAX_WATCHED):
    if not universe: return []
    by_volume = sorted(universe, key=lambda s: quotes.get(s, {}).get("quote_volume", 0), reverse=True)
    return list(dict.fromkeys([s for s in PREFERRED if s in universe] + by_volume))[:count]


def parse_closed_kline(message, allowed, now=None):
    """Accept only true closed, timely, well-formed 15-minute USDM kline events."""
    try:
        obj = json.loads(message) if isinstance(message,(str,bytes)) else message
        if isinstance(obj,dict) and "data" in obj: obj = obj["data"]
        if not isinstance(obj,dict) or obj.get("e") != "kline":return None
        symbol = obj.get("s")
        if symbol not in allowed: return None
        k = obj.get("k")
        if not isinstance(k,dict) or k.get("x") is not True or k.get("i") != "15m" or k.get("s") != symbol: return None
        now = time.time() if now is None else now
        event = int(obj["E"])
        opentime,closetime = int(k["t"]), int(k["T"])
        if abs(now*1000-event)>120000:return None
        if opentime%INTERVAL_MS!=0 or closetime!=opentime+INTERVAL_MS-1:return None
        if closetime>now*1000 or abs(now*1000-closetime)>120000:return None
        op,hi,lo,cl,vol=(float(k[n]) for n in ("o","h","l","c","v"))
        if not all(map(math.isfinite,(op,hi,lo,cl,vol))):return None
        if min(op,lo,cl)<=0 or vol<0 or hi<max(op,cl,lo) or lo>min(op,cl) or hi<lo:return None
        return symbol,{"time":opentime,"open":op,"high":hi,"low":lo,"close":cl,"volume":vol,"close_time":closetime}
    except (TypeError,KeyError,ValueError,OverflowError):return None


def store_bars(db,symbol,bars):
    """Store true historical or streamed completed OHLCV without replacing existing rows."""
    values=[]
    now_ms = int(time.time()*1000)
    for x in bars:
        try:
            t,tclose=int(x["time"]),int(x["close_time"])
            op,hi,lo,cl,vol=(float(x[k]) for k in ("open","high","low","close","volume"))
            if t%INTERVAL_MS or tclose!=t+INTERVAL_MS-1 or tclose>=now_ms:continue
            if not all(map(math.isfinite,(op,hi,lo,cl,vol))) or min(op,lo,cl)<=0 or hi<max(op,cl,lo) or lo>min(op,cl) or vol<0:continue
            values.append((symbol,t,op,hi,lo,cl,vol,tclose))
        except (ValueError,TypeError,KeyError):continue
    if values:
        db.executemany("INSERT OR IGNORE INTO completed_klines VALUES (?,?,?,?,?,?,?,?)",values)
        db.commit()
    return len(values)


async def seed_recent_rest(rest_client, db, symbol):
    """Bridge missing current-day candles with one paced 120-bar Binance REST read.

    BinanceClient owns all rate-limit/cooldown behavior. Only completed authentic
    candles persist; no current forming candles or synthetic candles are accepted.
    """
    bars = await rest_client.klines(symbol, limit=120)
    return store_bars(db, symbol, bars)


def contiguous_recent(db,symbol,needed=80,limit=180):
    rows=db.execute("SELECT open_time,open,high,low,close,volume,close_time FROM completed_klines WHERE symbol=? ORDER BY open_time DESC LIMIT ?",(symbol,limit)).fetchall()
    candles=[{"time":r[0],"open":r[1],"high":r[2],"low":r[3],"close":r[4],"volume":r[5],"close_time":r[6]} for r in reversed(rows)]
    if not candles:return []
    start=len(candles)-1
    while start>0 and candles[start]["time"]-candles[start-1]["time"]==INTERVAL_MS:start-=1
    segment=candles[start:]
    if len(segment)<needed:return []
    return segment


def freshness_ok(candles,now_ms=None,max_lag_ms=35*60*1000):
    if not candles:return False
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    return 0 <= now_ms-int(candles[-1]["close_time"]) <= max_lag_ms


def stream_groups(symbols, group_size=STREAMS_PER_SOCKET):
    """Stable deterministic bounded subscriptions. Every eligible symbol once."""
    size = max(1, int(group_size))
    return [symbols[i:i+size] for i in range(0, len(symbols), size)]


def might_be_closed(message):
    """Cheap rejection of ~4/sec still-forming kline updates before JSON parsing.

    All accepted messages still pass strict parse_closed_kline checks.
    """
    if not isinstance(message, (str, bytes)):
        return True
    return b'"x"' in message and b'true' in message if isinstance(message,bytes) else '"x"' in message and 'true' in message


async def _socket_group(group, on_closed, state_change, group_id):
    retry = 3.0
    allowed = set(group)
    streams = "/".join(quote(f"{sym.lower()}@kline_15m", safe="@_") for sym in group)
    url = WS_ENDPOINT + "?streams=" + streams
    while True:
        try:
            async with websockets.connect(url, ping_interval=25, ping_timeout=25,
                    open_timeout=20, close_timeout=8, max_size=256_000,
                    max_queue=64, compression=None) as ws:
                state_change(group_id, True)
                retry = 3.0
                deadline = time.monotonic() + 3500
                while time.monotonic() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                    except asyncio.TimeoutError:
                        continue
                    if not might_be_closed(msg):
                        continue
                    event = parse_closed_kline(msg, allowed)
                    if event:
                        on_closed(*event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Closed kline WS group %s unavailable: %s", group_id,str(exc)[:180])
        finally:
            state_change(group_id, False)
        await asyncio.sleep(retry + random.random())
        retry = min(240, retry * 2)


async def kline_worker(get_symbols, on_closed, on_state):
    """Track all eligible perpetuals across bounded WebSocket groups.

    A group reconnects independently so one transport failure does not drop
    every symbol. All streams are still filtered for authentic 15m closes.
    Membership is reconciled hourly as listings/volumes change.
    """
    while True:
        symbols = list(dict.fromkeys(get_symbols()))[:MAX_WATCHED]
        if not symbols:
            on_state("Sembol listesi bekleniyor",0)
            await asyncio.sleep(10)
            continue
        groups = stream_groups(symbols)
        active = set()
        def group_status(group_id, up):
            if up: active.add(group_id)
            else: active.discard(group_id)
            online = sum(len(groups[g]) for g in active)
            message = "Bağlandı" if online == len(symbols) else f"Kısmi bağlantı: {online}/{len(symbols)}"
            if online == 0: message = "Bağlantı bekleniyor"
            on_state(message, online)
        jobs = [asyncio.create_task(_socket_group(group,on_closed,group_status,idx))
                for idx,group in enumerate(groups)]
        try:
            await asyncio.sleep(3600)
        finally:
            for job in jobs: job.cancel()
            for job in jobs:
                try: await job
                except asyncio.CancelledError: pass
            on_state("Yeni sözleşmeler bağlanıyor",0)
