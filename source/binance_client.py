"""Rate-aware Binance USD-M Futures REST client with a shared IP-ban circuit breaker.

No exchange credentials. Binance may block shared cloud IPs; never bypass a ban by
rotating IPs and never hammer the endpoint after HTTP 418.
"""
import asyncio
import logging
import json
import os
from pathlib import Path
import math
import time
from typing import Any
import httpx
from symbol_utils import symbol_valid

LOG = logging.getLogger(__name__)
BASE_URL = "https://fapi.binance.com"

class BinanceError(Exception):
    pass

class BinanceRateLimited(BinanceError):
    def __init__(self, retry_seconds):
        self.retry_seconds = max(1, int(retry_seconds))
        super().__init__(f"Binance erişimi {self.retry_seconds} saniye duraklatıldı (418/429).")


class BinanceClient:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=BASE_URL, timeout=18.0,
                                        headers={"User-Agent": "CryptoAI-Pro-MVP/2.0"})
        # Shared pacing/circuit breaker across scanner, pricing, training and chart views.
        self.lock = asyncio.Lock()
        self.minimum_spacing = 2.1  # seconds per REST request; conservative shared-IP behavior
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.block_count = 0
        self.state_dir = Path(os.environ.get("DATA_DIR", "./data"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cooldown_path = self.state_dir / "binance_cooldown.json"
        self.wall_blocked_until = 0.0
        # Persist Retry-After across redeploys. A restart must never evade Binance's cooldown.
        try:
            state = json.loads(self.cooldown_path.read_text(encoding="utf-8"))
            self.wall_blocked_until = max(0., float(state.get("blocked_until_unix", 0)))
            remaining = self.wall_blocked_until - time.time()
            self.blocked_until = time.monotonic() + max(0., remaining)
            if remaining > 0:
                LOG.warning("Restored Binance REST cooldown: %.0fs", remaining)
        except (OSError, ValueError, TypeError):
            pass
        self.cache = {}
        self.cache_limit = 1500

    @property
    def cooldown_seconds(self):
        return max(0, math.ceil(self.blocked_until - time.monotonic()),
                   math.ceil(self.wall_blocked_until - time.time()))

    def _persist_cooldown(self):
        temp = self.cooldown_path.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps({"blocked_until_unix":self.wall_blocked_until}),encoding="utf-8")
            os.replace(temp, self.cooldown_path)
        except OSError as exc:
            LOG.warning("Cooldown state not persisted: %s", exc)

    def _cache_key(self, endpoint, params):
        return (endpoint, tuple(sorted(params.items())))

    @staticmethod
    def _ttl(endpoint, params):
        if endpoint.endswith("exchangeInfo"):
            return 6 * 3600
        if endpoint.endswith("ticker/24hr"):
            return 75
        if endpoint.endswith("klines"):
            tf = params.get("interval", "15m")
            if tf == "15m":
                # Historical inference is candle-close-based: at most one fetch per window.
                return 15 * 60
            return 45
        return 0

    async def get(self, endpoint: str, **params) -> Any:
        key = self._cache_key(endpoint, params)
        now = time.monotonic()
        if self.cooldown_seconds:
            raise BinanceRateLimited(self.cooldown_seconds)
        cached = self.cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        async with self.lock:
            now = time.monotonic()
            if self.cooldown_seconds:
                raise BinanceRateLimited(self.cooldown_seconds)
            cached = self.cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
            await asyncio.sleep(max(0., self.next_request - now))
            self.next_request = time.monotonic() + self.minimum_spacing
            try:
                response = await self.client.get(endpoint, params=params)
            except httpx.HTTPError as exc:
                raise BinanceError(f"Binance bağlantısı: {str(exc)[:180]}") from exc
            if response.status_code in (418, 429):
                try:
                    retry_after = max(0, int(response.headers.get("Retry-After", "0")))
                except ValueError:
                    retry_after = 0
                self.block_count += 1
                # 418 signals an IP ban; do not spam retries. Honor Retry-After if present.
                base = min(1800, 120 * (2 ** min(4, self.block_count - 1))) if response.status_code == 418 else 90
                pause = max(base, retry_after)
                self.blocked_until = max(self.blocked_until, time.monotonic() + pause)
                self.wall_blocked_until = max(self.wall_blocked_until, time.time() + pause)
                self._persist_cooldown()
                LOG.warning("Binance %s: REST paused for %ss", response.status_code, pause)
                raise BinanceRateLimited(pause)
            if response.status_code >= 500:
                raise BinanceError(f"Binance geçici HTTP {response.status_code}")
            if response.is_error:
                raise BinanceError(f"Binance HTTP {response.status_code}: {response.text[:180]}")
            try:
                value = response.json()
            except ValueError as exc:
                raise BinanceError("Binance JSON yanıtı çözümlenemedi") from exc
            self.block_count = 0
            if self.wall_blocked_until:
                self.wall_blocked_until = 0.0
                self._persist_cooldown()
            ttl = self._ttl(endpoint, params)
            if ttl:
                if endpoint.endswith("klines") and params.get("interval") == "15m":
                    # The cache should never cross a 15m UTC candle-close boundary.
                    next_boundary = (int(time.time() // 900) + 1) * 900
                    ttl = min(ttl, max(1, next_boundary - time.time()))
                self.cache[key] = (time.monotonic() + ttl, value)
                if len(self.cache) > self.cache_limit:
                    expired = [k for k,(exp,_) in self.cache.items() if exp <= time.monotonic()]
                    for k in expired: self.cache.pop(k, None)
                    if len(self.cache) > self.cache_limit:
                        self.cache.pop(next(iter(self.cache)))
            return value

    async def exchange_symbols(self):
        data = await self.get("/fapi/v1/exchangeInfo")
        return [{"symbol": x["symbol"], "base": x.get("baseAsset", ""), "quote": x.get("quoteAsset", "")}
                for x in data.get("symbols", []) if x.get("contractType") == "PERPETUAL"
                and x.get("status") == "TRADING" and x.get("quoteAsset") in ("USDT", "USDC")]

    async def tickers(self):
        data = await self.get("/fapi/v1/ticker/24hr")
        return {x["symbol"]: {"price": float(x["lastPrice"]),
                              "change_pct": float(x["priceChangePercent"]),
                              "quote_volume": float(x["quoteVolume"])}
                for x in data if float(x.get("lastPrice", 0)) > 0}

    async def klines(self, symbol, interval="15m", limit=180, end_time=None):
        if interval not in ("5m", "15m", "1h", "4h"):
            raise ValueError("Desteklenmeyen mum periyodu")
        if not symbol_valid(symbol):
            raise ValueError("Geçersiz sembol")
        params = {"symbol":symbol, "interval":interval,
                  "limit":min(max(int(limit), 1), 1500)}
        if end_time is not None:
            if not isinstance(end_time, int) or end_time <= 0:
                raise ValueError("Invalid historical candle endTime")
            params["endTime"] = end_time
        raw = await self.get("/fapi/v1/klines", **params)
        return [{"time": int(x[0]), "open": float(x[1]), "high": float(x[2]),
                 "low": float(x[3]), "close": float(x[4]), "volume": float(x[5]),
                 "close_time": int(x[6])} for x in raw]

    async def close(self):
        await self.client.aclose()
