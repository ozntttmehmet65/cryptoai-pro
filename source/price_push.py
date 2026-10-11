"""Bounded, authentic Binance ticker deltas for responsive browser updates."""
from __future__ import annotations
import math
import time


def price_delta(universe, tickers, now=None, since=0.0, max_age_seconds=120.0):
    """Return [price, 24h change %, quote volume, Binance event timestamp].

    Every quote must be fresh and from an eligible, listed contract. Never
    synthesize values or claim old snapshot prices are current.
    """
    now = time.time() if now is None else float(now)
    result = {}
    for symbol in universe:
        row = tickers.get(symbol)
        if not isinstance(row, dict):
            continue
        try:
            timestamp = float(row["updated_at"])
            price, change, volume = (float(row[k]) for k in
                                     ("price", "change_pct", "quote_volume"))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not (0.0 <= now - timestamp < max_age_seconds and timestamp > since):
            continue
        if not (all(math.isfinite(x) for x in (timestamp, price, change, volume))
                and price > 0 and volume >= 0):
            continue
        result[symbol] = [price, change, volume, timestamp]
    return result
