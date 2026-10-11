"""Single Binance public market WebSocket for USD-M Futures 24h tickers.

The stream only includes symbols which changed, so per-symbol update timestamps
are mandatory. Connection failures never produce fabricated quotes.
"""
from __future__ import annotations
import asyncio
import json
import math
import logging
import random
import time

import websockets

LOG=logging.getLogger(__name__)
WS_URL="wss://fstream.binance.com/market/ws/!ticker@arr"


def parse_tickers(payload, allowed, now=None):
    """Pure function for robust server-side filtering and testability."""
    try: data=json.loads(payload) if isinstance(payload,(str,bytes)) else payload
    except (ValueError,TypeError):return {}
    if isinstance(data,dict):data=data.get("data",[])
    if not isinstance(data,list):return {}
    now=time.time() if now is None else now
    out={}
    for x in data:
        if not isinstance(x,dict):continue
        s=x.get("s")
        if not isinstance(s,str) or s not in allowed or x.get("st",1)!=1:continue
        try:
            price=float(x["c"]);change=float(x["P"]);volume=float(x["q"])
            event_ms=int(x.get("E",int(now*1000)))
            # Reject historical/replayed future events. Event is needed for accurate freshness.
            if abs(now-event_ms/1000)>90:continue
            if not all(math.isfinite(v) for v in (price,change,volume)) or price<=0 or volume<0:continue
            out[s]={"price":price,"change_pct":change,"quote_volume":volume,"updated_at":event_ms/1000}
        except (TypeError,ValueError,KeyError,OverflowError):continue
    return out


async def ticker_worker(allowed_symbols, on_update, on_status):
    """Keep one stream open; reconnect with exponential backoff, no rapid hammering."""
    wait=3.0
    while True:
        try:
            async with websockets.connect(WS_URL,ping_interval=25,ping_timeout=25,
                                          max_size=3_000_000,close_timeout=7,open_timeout=18) as ws:
                on_status("Bağlandı")
                wait=3
                async for message in ws:
                    parsed=parse_tickers(message,allowed_symbols())
                    if parsed:on_update(parsed)
        except asyncio.CancelledError:raise
        except Exception as exc:
            LOG.warning("Binance WebSocket unavailable: %s",str(exc)[:180])
            on_status("Bağlantı bekleniyor")
        await asyncio.sleep(wait + random.random())
        wait=min(300.0,wait*2)
