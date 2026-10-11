"""CryptoAI Pro: no fake market values, no trading, real ML only when trained."""
from __future__ import annotations
import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import logging
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from binance_client import BinanceClient, BinanceError, BinanceRateLimited
from archive_client import ArchiveClient, last_complete_month
from market_stream import ticker_worker
from live_candles import (kline_worker, selected_symbols, setup as setup_live,
                          store_bars, contiguous_recent, freshness_ok, seed_recent_rest)
from paper_trade import setup as setup_paper, observe as observe_paper, settle as settle_paper, reconcile_quotes, report as paper_report
from quality_guard import live_drift_block
from execution_guard import guarded_prediction
from ml_engine import ModelEngine
from unified_scoring import combined_assessment
from coin_models import CoinModelRegistry
from symbol_utils import symbol_file_key
from price_push import price_delta

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cryptoai")
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
TRAIN_SYMBOLS = max(3, min(int(os.environ.get("TRAIN_SYMBOLS", "30")), 45))
TRAIN_CANDLES = max(1500, min(int(os.environ.get("TRAIN_CANDLES", "4500")), 4500))
SCAN_INTERVAL = max(20., float(os.environ.get("SCAN_INTERVAL_SECONDS", "25")))
ENABLE_REST_SCANNER = os.environ.get("ENABLE_REST_SCANNER", "false").lower() == "true"
AUTO_TRAIN = os.environ.get("AUTO_TRAIN", "true").lower() == "true"
TRAIN_INTERVAL_HOURS = max(6, min(float(os.environ.get("TRAIN_INTERVAL_HOURS", "24")), 168))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
# Independently trained models rotate around the clock, one contract at a time.
COIN_AUTO_TRAIN = os.environ.get("COIN_AUTO_TRAIN", "true").lower() == "true"
COIN_RETRAIN_HOURS = max(4, min(float(os.environ.get("COIN_RETRAIN_HOURS", "6")), 72))
COIN_RETRY_SECONDS = max(600, min(int(os.environ.get("COIN_RETRY_SECONDS", "3600")), 86400))

client = BinanceClient()
archive = ArchiveClient(DATA_DIR)
engine = ModelEngine(str(DATA_DIR))  # Historical global artifact; NEVER a V5.6 signal fallback.
coin_engine = CoinModelRegistry(str(DATA_DIR), max_in_memory=8)
SNAPSHOT_PATH = DATA_DIR / "last_market_snapshot.json"
TRAINING_CACHE = DATA_DIR / "training_history"
TRAINING_CACHE.mkdir(parents=True, exist_ok=True)

universe: dict[str, dict] = {}
tickers: dict[str, dict] = {}
signals: dict[str, dict] = {}
status: dict[str, Any] = {"universe_updated": None, "tickers_updated": None,
                          "last_error": None, "model_training": False, "training_stage": "Başlatılmadı",
                          "scan_count": 0, "last_scan": None, "ticker_source": "Bekleniyor",
                          "archive_retry_not_before": 0, "kline_stream": "Bekleniyor",
                          "live_watch_count": 0, "closed_kline_events": 0,
                          "live_signals": 0, "daily_history_ready": 0, "rest_bootstrap_count": 0,
                          "history_attempted": 0, "history_errors": 0, "last_closed_kline_at": None,
                          "coin_training_current": None, "coin_training_success": 0,
                          "coin_training_failed": 0, "coin_training_last": None,
                          "coin_last_failure": None, "coin_force_refresh": False,
                          "coin_failure_reasons": {}}
train_lock = asyncio.Lock()
db = sqlite3.connect(str(DATA_DIR / "signals.sqlite"), check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("CREATE TABLE IF NOT EXISTS signals (symbol TEXT, candle_time INTEGER, saved_at INTEGER, signal TEXT, "
           "confidence REAL, p_long REAL, p_short REAL, p_wait REAL, reference REAL, model_version TEXT, "
           "PRIMARY KEY(symbol, candle_time, model_version))")
db.commit()
setup_paper(db)
setup_live(db)
closed_queue = asyncio.Queue(maxsize=1200)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_float(v, default=0.):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _atomic_json(path: Path, value):
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Disk write failed %s: %s", path.name, exc)


def _load_snapshot():
    """Only preserve the universe and dated LAST-SEEN prices; never label old quotes as live."""
    global universe, tickers
    try:
        obj = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if not isinstance(obj.get("universe"), dict) or not isinstance(obj.get("tickers"), dict):
            return
        universe = obj["universe"]
        tickers = obj["tickers"]
        status["universe_updated"] = float(obj.get("universe_updated") or 0)
        status["tickers_updated"] = float(obj.get("tickers_updated") or 0)
        log.info("Restored market universe %s symbols, last quotes at %s", len(universe), status["tickers_updated"])
    except (OSError, ValueError, TypeError) as exc:
        log.info("No usable dated market snapshot: %s", exc)


async def _training_bars(symbol: str):
    """Use up to 4500 genuine 15m candles; fetch older pages sequentially.

    Binance REST caps a page at 1500; older data must be fetched with endTime,
    and all requests use the existing global 2.1s limiter / circuit breaker.
    New listings never get fabricated candles. Failed older-history attempts
    are cached for 24h instead of hammering the Futures endpoint.
    """
    f = TRAINING_CACHE / (symbol_file_key(symbol) + ".json")
    saved = []
    saved_at = 0
    previous_attempt = 0
    previous_target = 0
    try:
        obj = json.loads(f.read_text(encoding="utf-8"))
        if obj.get("symbol") == symbol:
            saved = obj["candles"]
            saved_at = int(obj.get("fetched_at", 0))
            previous_attempt = int(obj.get("older_backfill_attempted_at",0))
            previous_target = int(obj.get("older_backfill_target_count", 3000 if previous_attempt else 0))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    now=time.time()
    age=now-saved_at
    # With a valid cache, a restart must not repeat an enormous REST scan.
    if saved and 0<=age<3*3600 and len(saved)>=TRAIN_CANDLES:
        return saved[-TRAIN_CANDLES:]
    merged=list(saved)
    if not saved or age>=3*3600 or len(saved)<1500:
        recent_count=(min(TRAIN_CANDLES,1500)
                      if not saved or len(saved)<1500 or TRAIN_CANDLES>1500
                      else min(1500,max(160,int(max(0,age)/900)+35)))
        latest=await client.klines(symbol,limit=recent_count)
        merged.extend(latest)
    now_ms=int(time.time()*1000)
    clean={int(b["time"]):b for b in merged if int(b["close_time"]) < now_ms}
    merged=[clean[k] for k in sorted(clean)][-TRAIN_CANDLES:]
    # Page backwards only once per day for immature listings or unavailable
    # history; never invent missing bars and never fetch already-seen timestamps.
    if (merged and TRAIN_CANDLES>1500 and len(merged)<TRAIN_CANDLES
            and (not previous_attempt or now-previous_attempt>=24*3600
                 or previous_target < TRAIN_CANDLES)):
        previous_attempt=int(now)
        previous_target=TRAIN_CANDLES
        for _ in range(3):  # bounded: max 4500 candles -> at most 2 older pages
            if len(merged)>=TRAIN_CANDLES:
                break
            oldest=int(merged[0]["time"])
            earlier=await client.klines(symbol, limit=min(1500,TRAIN_CANDLES-len(merged)),
                                        end_time=oldest-1)
            extra={int(b["time"]):b for b in earlier
                   if int(b["close_time"]) < oldest and int(b["time"]) not in clean}
            if not extra:
                break
            clean.update(extra)
            merged=[clean[k] for k in sorted(clean)][-TRAIN_CANDLES:]
            if len(earlier)<min(1500,TRAIN_CANDLES-len(merged)) and len(extra)<100:
                break
    _atomic_json(f,{"symbol":symbol,"fetched_at":int(now),
                    "older_backfill_attempted_at":previous_attempt,
                    "older_backfill_target_count":previous_target,"candles":merged})
    return merged


async def refresh_market():
    """Only exchange metadata uses REST; quote stream comes from one WebSocket."""
    global universe
    if client.cooldown_seconds:
        return
    try:
        if not universe or not status["universe_updated"] or time.time()-status["universe_updated"] > 21600:
            symbols = await client.exchange_symbols()
            new_universe = {s["symbol"]: s for s in symbols}
            if not new_universe:
                raise BinanceError("Uygun işlem gören Futures sözleşmesi bulunamadı")
            universe = new_universe
            status["universe_updated"] = time.time()
            for stale in list(signals):
                if stale not in universe:del signals[stale]
        status["last_error"] = None
    except Exception as exc:
        status["last_error"] = str(exc)
        log.warning("Cannot refresh symbol universe: %s", exc)


def _stream_prices(updates):
    # Merge changed symbols; global last update is not an assertion that all quotes are fresh.
    for symbol, quote in updates.items():
        tickers[symbol]=quote
    status["tickers_updated"] = max(q["updated_at"] for q in updates.values())
    # Evaluate TP/SL at each incoming Binance ticker batch, not on the next
    # 15m candle or a 3-second polling timer. This remains observational only.
    try:
        changed = reconcile_quotes(db, updates, int(time.time()*1000))
        if changed:
            _drift_cache.clear()
            log.info("TP/SL paper results updated: %s",changed)
    except Exception as exc:
        log.warning("TP/SL ticker observation failed: %s",str(exc)[:180])


def _stream_status(value):
    status["ticker_source"] = "WebSocket: " + value


def _live_symbols():
    return selected_symbols(universe, tickers)


def _kline_state(message, count):
    status["kline_stream"] = message
    status["live_watch_count"] = count


def _on_kline_close(symbol, candle):
    # Executed in the asyncio event loop; SQLite write is bounded and non-async.
    store_bars(db, symbol, [candle])
    # Settlement must continue even when ML is unavailable or now says BEKLE.
    # Hypothetical exits have NO holding-time limit; only observed TP/SL first touch.
    status["closed_kline_events"] += 1
    status["last_closed_kline_at"] = utc_now()
    enqueue_inference(symbol, "WebSocket kapanmış 15dk")


def enqueue_inference(symbol: str, source: str):
    """Only real closed candles can enter the bounded inference queue."""
    try:
        closed_queue.put_nowait((symbol, source))
    except asyncio.QueueFull:
        log.warning("Closed candle processing queue full: %s", symbol)


async def history_warm_worker():
    """Paced one-off REST bootstrap for the *entire* watched universe.

    Avoid 3 archive downloads per symbol before trying the simpler 120-bar
    official REST history. A single BinanceClient enforces a 2.1s global gap
    and a persisted 418/429 circuit breaker; no IP-ban circumvention.
    Existing disk history is reused first, so routine restarts do NOT repeat
    hundreds of requests. Missing days fall back to published ZIP files only.
    """
    from datetime import timedelta
    attempts = {}
    archive_attempts = {}
    while True:
        symbols = _live_symbols()
        ready = 0
        for idx, symbol in enumerate(symbols):
            candles = contiguous_recent(db, symbol, 80)
            if freshness_ok(candles):
                ready += 1
                # Make newly started service useful immediately from persisted
                # genuine closed bars; do not invent interim candles.
                if symbol not in signals and coin_engine.ready(symbol):
                    enqueue_inference(symbol, "Kaydedilmiş kapanmış 15dk mumlar")
                status["daily_history_ready"] = ready
                continue
            now = time.time()
            # Rest history is not a trading order. Do not retry any symbol for
            # 90 minutes; pacing and 418/429 protection are shared globally.
            if not client.cooldown_seconds and now-attempts.get(symbol,0) >= 5400:
                attempts[symbol] = now
                status["history_attempted"] += 1
                try:
                    inserted = await seed_recent_rest(client, db, symbol)
                    if inserted:
                        status["rest_bootstrap_count"] += 1
                    if freshness_ok(contiguous_recent(db, symbol, 80)):
                        ready += 1
                        status["daily_history_ready"] = ready
                        enqueue_inference(symbol, "Binance REST tamamlanmış 15dk mumlar")
                        log.info("History ready: %s (%s completed bars)", symbol, inserted)
                        continue
                except BinanceRateLimited as exc:
                    status["history_errors"] += 1
                    log.warning("History REST paused: %s seconds", exc.retry_seconds)
                except Exception as exc:
                    status["history_errors"] += 1
                    log.warning("History REST failed %s: %s", symbol,str(exc)[:130])
                await asyncio.sleep(0.25)
            # Official daily ZIP fallback; cannot synthesize missing current day.
            # Avoid fetching thousands of ZIPs during a temporary exchange ban.
            if client.cooldown_seconds and idx < 120:
                for back in (2, 1):
                    day = (datetime.now(timezone.utc).date()-timedelta(days=back)).isoformat()
                    key = (symbol,day)
                    if now-archive_attempts.get(key,0)<7200:continue
                    archive_attempts[key] = now
                    try: store_bars(db, symbol, await archive.day_candles(symbol,day))
                    except FileNotFoundError: pass
                    except Exception as exc:
                        log.warning("Daily archive %s %s: %s", symbol,day,str(exc)[:100])
                    await asyncio.sleep(0.75)
                if freshness_ok(contiguous_recent(db,symbol,80)):
                    ready += 1
                    status["daily_history_ready"] = ready
                    enqueue_inference(symbol, "Binance resmî arşiv kapanmış 15dk mumlar")
            if idx % 16 == 0:
                status["daily_history_ready"] = ready
            await asyncio.sleep(0.03)
        status["daily_history_ready"] = sum(1 for sym in symbols
                if freshness_ok(contiguous_recent(db,sym,80)))
        await asyncio.sleep(180)



_drift_cache = {}

def _current_drift(side, now_ms):
    cached = _drift_cache.get(side)
    if cached and 0 <= now_ms - cached[0] < 1000:
        return cached[1]
    value = live_drift_block(db, side, now_ms)[0]
    _drift_cache[side] = (now_ms, value)
    return value


def _guard(symbol, pred, now_ms):
    side = pred.get('signal')
    permission = coin_engine.permissions(symbol).get(side, {}).get('approved', False)
    return guarded_prediction(pred, tickers.get(symbol), now_ms, permission,
                              _current_drift(side, now_ms) if side in ('LONG', 'SHORT') else False)


def _active_prediction(symbol, now_ms):
    pred = signals.get(symbol)
    if (not pred or pred.get('model_version') != coin_engine.version(symbol)
        or not 0 <= now_ms - pred.get('candle_time', 0) < 45*60*1000
        or (client.cooldown_seconds and not pred.get('source', '').startswith('WebSocket'))):
        return None
    return _guard(symbol, pred, now_ms)


def _save_signal(symbol, inf, bars, now_ms, source):
    inf["symbol"] = symbol
    inf["updated_at"] = utc_now()
    inf["source"] = source
    signals[symbol] = inf
    db.execute("INSERT OR IGNORE INTO signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (symbol,inf["candle_time"],now_ms,inf["signal"],
                inf["confidence"],inf["p_long"],inf["p_short"],inf["p_wait"],
                inf["entry_reference"],inf["model_version"]))
    db.commit()
    inf = _guard(symbol, inf, now_ms)
    quote=tickers.get(symbol)
    if quote and 0<=time.time()-quote.get("updated_at",0)<10:
        if inf["signal"] in ("LONG", "SHORT"):
            observe_paper(db,symbol,inf,quote.get("price"),now_ms)
        elif inf.get("blocked_reason") and inf.get("candidate_signal") in ("LONG", "SHORT"):
            candidate = dict(inf,signal=inf["candidate_signal"],confidence=inf.get("p_long")
                             if inf["candidate_signal"]=="LONG" else inf.get("p_short"))
            observe_paper(db,symbol,candidate,quote.get("price"),now_ms,shadow=True)
    status["scan_count"] += 1
    status["last_scan"] = utc_now()


async def stream_signal_worker():
    """REST-block independent inference on consecutive streamed/archive candles."""
    while True:
        item = await closed_queue.get()
        symbol, source = item if isinstance(item, tuple) else (item, "WebSocket kapanmış 15dk")
        try:
            if symbol not in universe or not coin_engine.ready(symbol):
                continue
            candles=contiguous_recent(db,symbol,80,limit=500)
            if not freshness_ok(candles):continue
            result=coin_engine.infer(symbol,candles)
            if result is None:continue
            now_ms=int(time.time()*1000)
            # Do not emit a duplicated prediction for an already assessed candle.
            previous = signals.get(symbol)
            if previous and previous.get("candle_time") == result.get("candle_time") and previous.get("model_version") == result.get("model_version"):
                continue
            _save_signal(symbol,result,candles,now_ms,source)
            status["live_signals"] += 1
        except Exception as exc:
            log.warning("WebSocket inference %s: %s",symbol,str(exc)[:180])
        finally:closed_queue.task_done()


def reconcile_paper(now_ms: int):
    """Backup reconciliation; first TP/SL touch is tracked at quote ingestion."""
    changed = reconcile_quotes(db, tickers, now_ms)
    if changed:
        _drift_cache.clear()
        log.info("TP/SL paper results updated: %s", changed)
    return changed


async def paper_settlement_worker():
    """Reconcile incoming TP/SL quote observations; never time-force an exit."""
    while True:
        try:
            reconcile_paper(int(time.time()*1000))
        except Exception as exc:
            log.warning("Paper quote settlement failed: %s", str(exc)[:180])
        await asyncio.sleep(3)


async def market_worker():
    last_snapshot=0.0
    while True:
        if not client.cooldown_seconds:
            await refresh_market()
        if time.monotonic() - last_snapshot >= 120 and universe and tickers:
            _atomic_json(SNAPSHOT_PATH,{"universe":universe,"tickers":tickers,
                         "universe_updated":status["universe_updated"],
                         "tickers_updated":status["tickers_updated"]})
            last_snapshot=time.monotonic()
        await asyncio.sleep(75)


async def train_model():
    async with train_lock:
        if not universe:
            await refresh_market()
        if not universe:
            status["training_stage"]="Borsa sembol listesi yok; arşiv eğitimi başlatılamadı"
            return
        status["model_training"] = True
        status["training_stage"] = "Eğitim için geçmiş 15 dakikalık mumlar hazırlanıyor"
        try:
            preferred = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT"]
            ranked = sorted(universe, key=lambda sym: tickers.get(sym, {}).get("quote_volume", 0), reverse=True)
            order = list(dict.fromkeys([s for s in preferred if s in universe] + ranked))
            by_symbol = {}
            source = "Binance Futures REST"
            use_archive=bool(client.cooldown_seconds)
            for symbol in order[:TRAIN_SYMBOLS]:
                try:
                    if use_archive:
                        status["training_stage"]="Resmî Binance geçmiş veri arşivinden model eğitimi"
                        bars=await archive.month_candles(symbol)
                        source="Binance USD-M aylık arşiv (gecikmeli)"
                    else:
                        try:
                            bars=await _training_bars(symbol)
                        except BinanceRateLimited:
                            use_archive=True
                            bars=await archive.month_candles(symbol)
                            source="Binance USD-M aylık arşiv (gecikmeli)"
                    if len(bars)>=380:by_symbol[symbol]=bars
                except (OSError,ValueError,HTTPException) as exc:
                    log.warning("Training history %s unavailable: %s",symbol,exc)
                except Exception as exc:
                    log.warning("Training history %s unavailable: %s",symbol,exc)
                await asyncio.sleep(0.4)
            if len(by_symbol)<3:
                raise RuntimeError("Arşiv veya REST verilerinden yeterli üç coin toplanamadı")
            status["training_stage"] = f"{len(by_symbol)} coin üzerinde gerçek ML eğitimi / kronolojik test"
            info = await asyncio.to_thread(engine.fit, by_symbol)
            info["training_data_source"] = source
            # engine.fit persists its original info: persist provenance as well.
            _atomic_json(engine.meta_path, info)
            status["training_stage"] = f"Eğitim tamamlandı — {info['version']} ({source})"
            status["last_error"] = None
        except Exception as exc:
            status["training_stage"] = f"Eğitim tamamlanamadı: {str(exc)[:160]}"
            status["last_error"] = str(exc)
            status["archive_retry_not_before"] = time.time()+3600
            log.warning("Training deferred: %s",exc)
        finally:
            status["model_training"] = False


async def scan_worker():
    last_model_version = None
    order = []
    pos = 0
    while True:
        if not universe or not tickers:
            await asyncio.sleep(4)
            continue
        if client.cooldown_seconds:
            await asyncio.sleep(min(900, client.cooldown_seconds + 1))
            continue
        version = "coin-registry-v1"
        if pos >= len(order) or version != last_model_version:
            order = sorted(universe, key=lambda s: tickers.get(s, {}).get("quote_volume", 0), reverse=True)
            pos = 0
            last_model_version = version
        if not order:
            await asyncio.sleep(5)
            continue
        symbol = order[pos]
        try:
            # Resolve older paper observations using only subsequent completed candles.
            bars = await client.klines(symbol, limit=110)
            now_ms = int(time.time()*1000)
            settle_paper(db, symbol, bars, now_ms)
            inf = coin_engine.infer(symbol,bars)
            if inf is not None:
                _save_signal(symbol, inf, bars, now_ms, "Binance REST kapanmış 15dk")
            pos += 1
        except BinanceRateLimited as exc:
            # Keep current symbol queued. A single 418 must not become hundreds of retries.
            await asyncio.sleep(min(1800, exc.retry_seconds + 1))
            continue
        except Exception as exc:
            log.warning("Signal scan %s failed: %s", symbol, exc)
            pos += 1
        await asyncio.sleep(SCAN_INTERVAL)


async def coin_training_worker():
    """Forever-running, fair and strictly SEQUENTIAL per-coin trainer.

    Each coin has a separate model and independent time-purged held-out test.
    Mature training history uses REST once per due coin through the EXISTING
    Binance rate limiter/circuit breaker. No redundant worker per symbol, no
    model trained from <119 startup bars, no fallback to the legacy global model.
    """
    last_attempt = {}
    failures = {}
    while True:
        if not COIN_AUTO_TRAIN or not universe:
            await asyncio.sleep(30)
            continue
        symbols = sorted(universe, key=lambda s: tickers.get(s,{}).get("quote_volume",0),reverse=True)
        force = bool(status["coin_force_refresh"])
        status["coin_force_refresh"] = False
        for symbol in symbols:
            if not COIN_AUTO_TRAIN:
                break
            if symbol not in universe:
                continue
            now = time.time()
            # Existing approved/unapproved model stays available if refreshing
            # its dataset fails; it is never overwritten with synthetic rows.
            if not force and not coin_engine.training_due(symbol, now, COIN_RETRAIN_HOURS):
                continue
            if now - last_attempt.get(symbol,0) < (COIN_RETRY_SECONDS if failures.get(symbol) else 600):
                continue
            if client.cooldown_seconds:
                status["training_stage"] = f"Binance dinlenme süresi · {client.cooldown_seconds} sn; mevcut modeller kullanılıyor"
                await asyncio.sleep(min(900,client.cooldown_seconds+1))
                break  # Do not wait once PER COIN during a shared Binance ban.
            last_attempt[symbol] = now
            status["coin_training_current"] = symbol
            status["model_training"] = True
            status["training_stage"] = f"7/24 coin bazli model egitimi: {symbol}"
            try:
                bars = await _training_bars(symbol)
                info = await asyncio.to_thread(coin_engine.fit, symbol, bars)
                status["coin_training_success"] += 1
                status["coin_training_last"] = {"symbol":symbol,"version":info["version"],
                                                 "at":info["trained_at"]}
                status["coin_last_failure"] = None
                failures.pop(symbol,None)
                status["coin_failure_reasons"].pop(symbol, None)
                # Training is independent of the 15-minute inference queue;
                # model is available immediately for the last CLOSED candle.
                if freshness_ok(contiguous_recent(db,symbol,80)):
                    enqueue_inference(symbol,"Coin modeli son egitim ve kapanmis 15dk")
                log.info("Independent model trained %s %s, LONG=%s SHORT=%s", symbol,
                         info["version"], coin_engine.permissions(symbol)["LONG"]["approved"],
                         coin_engine.permissions(symbol)["SHORT"]["approved"])
            except BinanceRateLimited as exc:
                failures[symbol] = True
                status["coin_training_failed"] += 1
                status["coin_last_failure"] = f"{symbol}: Binance REST cooldown {exc.retry_seconds}s"
                status["coin_failure_reasons"][symbol] = "Binance REST cooldown"
                status["training_stage"] = f"Binance dinlenme süresi · {exc.retry_seconds} sn"
                await asyncio.sleep(min(900,exc.retry_seconds+1))
                break  # One 418/429 suspends the whole coin-training batch.
            except Exception as exc:
                failures[symbol] = True
                status["coin_training_failed"] += 1
                status["coin_last_failure"] = f"{symbol}: {str(exc)[:160]}"
                status["coin_failure_reasons"][symbol] = str(exc)[:160]
                log.info("Coin training postponed %s: %s",symbol,str(exc)[:160])
            finally:
                status["coin_training_current"] = None
                status["model_training"] = False
            await asyncio.sleep(0.15)  # REST gateway already handles 2.1s pacing
        status["training_stage"] = (f"7/24 per-coin training active: "
                                     f"{coin_engine.stats(universe)['trained']} / {len(universe)} trained")
        await asyncio.sleep(180)


async def auto_training_worker():
    """Retry startup training and retrain stale models without extra web workers."""
    while True:
        try:
            if AUTO_TRAIN and universe and time.time() >= status["archive_retry_not_before"] and not status["model_training"]:
                trained_at = (engine.info or {}).get("trained_at")
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(trained_at)).total_seconds() if trained_at else float("inf")
                except (TypeError, ValueError):
                    age = float("inf")
                if (engine.model is None or age >= TRAIN_INTERVAL_HOURS * 3600
                    or (engine.info or {}).get('evaluation_method') != 'tp_sl_first_touch_24h_v1'):
                    await train_model()
        except Exception as exc:
            log.exception("Otomatik model egitimi basarisiz: %s", exc)
        await asyncio.sleep(180)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_snapshot()
    status["training_stage"] = (f"7/24 coin bazli egitim — "
                                 f"{coin_engine.stats(universe)['trained']} ayrik model diskten bulundu")
    tasks = [asyncio.create_task(market_worker()),
             asyncio.create_task(coin_training_worker()),
             asyncio.create_task(ticker_worker(lambda: set(universe), _stream_prices, _stream_status)),
             asyncio.create_task(kline_worker(_live_symbols, _on_kline_close, _kline_state)),
             asyncio.create_task(history_warm_worker()),
             asyncio.create_task(stream_signal_worker()),
             asyncio.create_task(paper_settlement_worker())]
    if ENABLE_REST_SCANNER:
        tasks.append(asyncio.create_task(scan_worker()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await client.close()
        await archive.close()
        db.close()


app = FastAPI(title="CryptoAI Pro", description="Binance Futures sinyal ve gerçek makine öğrenmesi tarayıcısı", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.get("/")
async def home():
    return FileResponse(str(ROOT / "static" / "index.html"))


def _unified_counts(now):
    stats={'islem_sinyali':0,'takip_et':0,'bekle':0,'long':0,'short':0}
    now_ms=int(now*1000)
    for sym in signals:
        p = _active_prediction(sym, now_ms)
        if p is None:
            continue
        unified=combined_assessment(p)
        st=unified['status']
        stats['islem_sinyali' if st=='İŞLEM SİNYALİ' else 'takip_et' if st=='TAKİP ET' else 'bekle']+=1
        if st!='BEKLE':
            stats['long' if unified['direction']=='LONG' else 'short']+=1
    return stats


@app.get("/api/health")
async def health():
    now = time.time()
    fresh = status["tickers_updated"] is not None and now - status["tickers_updated"] < 120
    return {"provider": "Binance USDⓈ-M Futures (WebSocket tickers + REST candles + official archive)", "provider_ok": fresh,
            "model_ready": coin_engine.stats(universe)["trained"] > 0,
            "model_training": status["model_training"],
            "training_stage": status["training_stage"], "last_error": status["last_error"],
            "coin_training": {**coin_engine.stats(universe), "enabled": COIN_AUTO_TRAIN,
                 "refresh_interval_hours": COIN_RETRAIN_HOURS,
                 "current_symbol": status["coin_training_current"],
                 "successful_this_start": status["coin_training_success"],
                 "failed_attempts_this_start": status["coin_training_failed"],
                 "last_success": status["coin_training_last"],
                 "last_failure": status["coin_last_failure"],
                 "pending_symbols": sorted(sym for sym in universe if not coin_engine.ready(sym)),
                 "pending_reasons": {sym: reason for sym, reason in status["coin_failure_reasons"].items()
                                      if sym in universe and not coin_engine.ready(sym)}},
            "symbol_count": len(universe),
            "quote_coverage": {"fresh": sum(1 for sym in universe if sym in tickers
                                      and 0 <= now - tickers[sym].get("updated_at", 0) < 120),
                               "missing": sorted(sym for sym in universe if sym not in tickers
                                      or not 0 <= now - tickers[sym].get("updated_at", 0) < 120)},
            "scanned_symbols": len(signals),
            "scan_count": status["scan_count"], "last_scan": status["last_scan"],
            "binance_cooldown_seconds": client.cooldown_seconds,
            "websocket": status["ticker_source"],
            "kline_websocket": status["kline_stream"],
            "live_watch_count": status["live_watch_count"],
            "watch_target_count": len(_live_symbols()),
            "history_attempted": status["history_attempted"],
            "history_errors": status["history_errors"],
            "last_closed_kline_at": status["last_closed_kline_at"],
            "closed_kline_events": status["closed_kline_events"],
            "live_signals": status["live_signals"],
            "daily_history_ready": status["daily_history_ready"],
            "rest_bootstrap_count": status["rest_bootstrap_count"],
            "rest_scanner_enabled": ENABLE_REST_SCANNER,
            "archive_train_retry_at": status["archive_retry_not_before"] or None,
            "storage_persistent_configured": os.environ.get("PERSISTENT_DISK", "false").lower() == "true",
            "last_market_snapshot_at": datetime.fromtimestamp(status["tickers_updated"],tz=timezone.utc).isoformat() if status["tickers_updated"] else None,
            "paper_observations": paper_report(db, 1)["summary"],
            "safety_gate_active": coin_engine.stats(universe)["both_directions_blocked"] > 0,
            "signal_decisions_ready": sum(1 for symbol,p in signals.items()
                if p.get("model_version") == coin_engine.version(symbol)
                and int(time.time()*1000) - p.get("candle_time",0) < 45*60*1000),
            "technical_watchlist": {"long":sum(1 for symbol,p in signals.items() if p.get("technical") and p["technical"].get("direction")=="LONG"
                                    and p.get("model_version")==coin_engine.version(symbol)
                                    and int(time.time()*1000)-p.get("candle_time",0)<45*60*1000),
                                     "short":sum(1 for symbol,p in signals.items() if p.get("technical") and p["technical"].get("direction")=="SHORT"
                                    and p.get("model_version")==coin_engine.version(symbol)
                                    and int(time.time()*1000)-p.get("candle_time",0)<45*60*1000)},
            "technical_feature_count":len(__import__('ml_engine').COLS),
            "unified_scoring_version":"V6.2",
            "unified_counts": _unified_counts(now),
            "data_updated_at": datetime.fromtimestamp(status["tickers_updated"], tz=timezone.utc).isoformat() if status["tickers_updated"] else None}


@app.get("/api/prices")
async def live_prices():
    """Read-only, small quote-only fallback for browsers without WebSocket support."""
    now = time.time()
    return {"type": "quotes", "quotes": price_delta(universe, tickers, now),
            "server_time": int(now * 1000), "source": status["ticker_source"]}


@app.websocket("/ws/prices")
async def stream_prices_to_browser(socket: WebSocket):
    """Push real quote changes ~1/sec; no Binance REST and no order endpoints.

    Reconnecting clients start with a bounded fresh snapshot. Each event includes
    its original Binance timestamp, allowing browsers to reject stale updates.
    """
    await socket.accept()
    last_timestamp = 0.0
    heartbeat = 0.0
    try:
        while True:
            now = time.time()
            updates = price_delta(universe, tickers, now, since=last_timestamp)
            if updates:
                await socket.send_json({"type": "quotes", "quotes": updates,
                                        "server_time": int(now * 1000),
                                        "source": status["ticker_source"]})
                last_timestamp = max(row[3] for row in updates.values())
                heartbeat = now
            elif now - heartbeat >= 10.0:
                await socket.send_json({"type": "heartbeat", "server_time": int(now * 1000),
                                        "source": status["ticker_source"]})
                heartbeat = now
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError, OSError):
        return


@app.get("/api/markets")
async def markets(search: str = "", signal: str = "HEPSI", sort: str = "volume",
                  limit: int = Query(1500, ge=1, le=2500)):
    fresh = status["tickers_updated"] is not None and time.time()-status["tickers_updated"] < 120
    rows = []
    watching = set(_live_symbols())
    for sym, entry in universe.items():
        if search and search.upper() not in sym:
            continue
        quote_candidate=tickers.get(sym, {})
        quote = quote_candidate if fresh and time.time()-quote_candidate.get("updated_at",0)<120 else {}
        active = _active_prediction(sym, int(time.time()*1000))
        row = {"symbol":sym, "base": entry["base"], "quote":entry["quote"],
               "price": quote.get("price"), "change_pct":quote.get("change_pct"),
               "quote_volume": quote.get("quote_volume"),
               "signal":active["signal"] if active else None,
               "confidence": active["confidence"] if active else None,
               "p_long": active["p_long"] if active else None,
               "p_short": active["p_short"] if active else None,
               "candle_time": active["candle_time"] if active else None,
               "coverage": "hazır" if active else "coin modeli hazırlanıyor / doğrulama bekleniyor" if sym in watching and not coin_engine.ready(sym) else "kapanmış mum bekleniyor" if sym in watching else "henüz izlenmiyor",
               "signal_generated_at": active.get("updated_at") if active else None,
               "candle_closed_at": datetime.fromtimestamp((active["candle_time"]+900000)/1000,tz=timezone.utc).isoformat() if active else None,
               "quote_updated_at": datetime.fromtimestamp(quote["updated_at"],tz=timezone.utc).isoformat() if quote else None,
               "decision_reason": active.get("blocked_reason") if active else None,
               "technical":active.get("technical") if active else None,
               "unified": combined_assessment(active) if active else combined_assessment(None)}
        if signal == "YON_LONG" and row["unified"].get("direction") != "LONG":
            continue
        if signal == "YON_SHORT" and row["unified"].get("direction") != "SHORT":
            continue
        if signal == "TAKIP" and row["unified"].get("status") != "TAKİP ET":
            continue
        if signal == "ONAYLI" and row["unified"].get("status") != "İŞLEM SİNYALİ":
            continue
        if signal == "TEKNIK_LONG" and not (row["technical"] and row["technical"].get("direction")=="LONG"):
            continue
        if signal == "TEKNIK_SHORT" and not (row["technical"] and row["technical"].get("direction")=="SHORT"):
            continue
        if signal == "BEKLE" and row["unified"].get("status") != "BEKLE":
            continue
        if signal not in ("HEPSI","TEKNIK_LONG","TEKNIK_SHORT","YON_LONG","YON_SHORT","TAKIP","ONAYLI","BEKLE") and row["signal"] != signal:
            continue
        rows.append(row)
    if sort == "name":
        rows.sort(key=lambda x: x["symbol"])
    elif sort == "change":
        rows.sort(key=lambda x: x["change_pct"] if x["change_pct"] is not None else -1e10, reverse=True)
    elif sort == "birlesik":
        rows.sort(key=lambda x: (x["unified"].get("status") == "İŞLEM SİNYALİ",
                                 x["unified"].get("status") == "TAKİP ET",
                                 x["unified"].get("score") or 0), reverse=True)
    elif sort == "technical":
        rows.sort(key=lambda x: x["technical"].get("score",0) if x["technical"] else -1, reverse=True)
    elif sort == "confidence":
        rows.sort(key=lambda x: x["confidence"] if x["confidence"] is not None else -1, reverse=True)
    else:
        rows.sort(key=lambda x: x["quote_volume"] if x["quote_volume"] is not None else -1, reverse=True)
    return {"total":len(rows), "rows":rows[:limit], "fresh":fresh,
            "updated_at": datetime.fromtimestamp(status["tickers_updated"], tz=timezone.utc).isoformat() if status["tickers_updated"] else None}


@app.get('/api/opportunities')
async def opportunities(limit: int = Query(12, ge=1, le=50)):
    """One scored LONG/SHORT view; never elevates unapproved TA to AI trade."""
    now=time.time();now_ms=int(now*1000)
    rows=[];counts={'islem_sinyali':0,'takip_et':0,'bekle':0}
    for sym in universe:
        pred = _active_prediction(sym, now_ms)
        q = tickers.get(sym)
        if pred is None or not q:
            continue
        combined=combined_assessment(pred)
        if combined['status']=='BEKLE':
            counts['bekle']+=1
            continue
        permitted=combined['approved']
        counts['islem_sinyali' if permitted else 'takip_et']+=1
        info=coin_engine.info(sym) or {}
        direction=combined['direction']
        holdout=(info.get('directional_validation') or {}).get(direction,{}) if permitted else ((pred.get('technical') or {}).get('holdout') or {})
        rows.append({'symbol':sym,'kind':combined['status'],'direction':direction,
                     'unified':combined,'ai_signal':pred['signal'],'price':q.get('price'),
                     'entry_reference':combined['entry_reference'],
                     'tp_estimate':combined['tp_estimate'],'sl_estimate':combined['sl_estimate'],
                     'ai_model_score':combined['ai_model_score'] if permitted else None,
                     'historical_tp_rate':holdout.get('hit_rate'),
                     'historical_resolved':holdout.get('resolved'),
                     'historical_stress_net':(holdout.get('mean_stressed_net_return') if permitted else holdout.get('mean_stressed_return')),
                     'historical_pass':permitted,
                     'technical_score':(pred.get('technical') or {}).get('score'),
                     'reasons':(pred.get('technical') or {}).get('reasons',[])[:4],
                     'entry_timing':pred.get('entry_timing') if permitted else (pred.get('technical') or {}).get('entry_timing'),
                     'updated_at':pred.get('updated_at'),
                     'warning':'Birleşik puan TP kazanma olasılığı değildir; takip fırsatı işlem onayı değildir'})
    rows.sort(key=lambda x:(x['kind']=='İŞLEM SİNYALİ',x['unified']['score']),reverse=True)
    return {'version':'V6.2','total':len(rows),'counts':counts,'rows':rows[:limit],
            'notice':'AI + indikatör + formasyon + 1s/4s tek puan ve tek yön. TAKİP ET doğrulanmış işlem sinyali değildir. 0-100 puan TP başarı yüzdesi değildir.'}


@app.get("/api/paper")
async def paper(limit: int = Query(100, ge=1, le=500)):
    """Forward paper observations only, never retrofitted historical trades."""
    return paper_report(db,limit)


@app.get("/api/model")
async def model_info():
    return {"ready": coin_engine.stats(universe)["trained"]>0,
            "training":status["model_training"], "stage":status["training_stage"],
            "scope":"independent_model_per_symbol",
            "coin_training":coin_engine.stats(universe),
            "legacy_global_model":{"exists":engine.model is not None,
                                  "version":(engine.info or {}).get("version"),
                                  "used_for_live_signals":False},
            "notice":"V6.0: Her coin icin 4500 gercek 15dk mum hedefi; TP/SL katsayilari yalnizca purged TRAIN doneminde secilir ve kronolojik VALID/TEST ayridir. Canli sanal pozisyonun TP/SL'si egitimde secilen planla aynidir; belirli saate gore kapanmaz. Dogrulanmamis teknik adaylar AI islemi degildir. Eski modeller korunur; gercek emir yok."}


@app.post("/api/admin/retrain")
async def retrain(x_admin_token: str | None = Header(default=None)):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Yönetici anahtarı zorunlu")
    status["coin_force_refresh"] = True
    return {"message": "563 coin icin sirali yeni egitim turu istendi; 7/24 gorev sonraki turda uygulayacak"}


@app.get("/api/coin-model/{symbol}")
async def coin_model_detail(symbol: str):
    symbol = symbol.upper()
    if symbol not in universe:
        raise HTTPException(status_code=404,detail="Desteklenmeyen Futures sozlesmesi")
    return {"symbol":symbol, "trained":coin_engine.ready(symbol),
            "info":coin_engine.info(symbol), "directional_signal_permissions":coin_engine.permissions(symbol),
            "notice":"Tek basina egitim basarili sinyal veya kar garantisi degildir."}


@app.get("/api/coin-models")
async def coin_model_list(limit: int = Query(100, ge=1, le=1000)):
    models = []
    for sym in sorted(universe):
        info = coin_engine.info(sym)
        if info:
            p = coin_engine.permissions(sym)
            models.append({"symbol":sym,"version":info["version"], "trained_at":info["trained_at"],
                          "approved_long":p["LONG"]["approved"], "approved_short":p["SHORT"]["approved"],
                          "test_samples":info.get("test_samples")})
    return {"stats":coin_engine.stats(universe),"models":models[:limit],"total_models":len(models)}


@app.get("/api/symbol/{symbol}")
async def symbol_detail(symbol: str):
    symbol = symbol.upper()
    if symbol not in universe:
        raise HTTPException(status_code=404, detail="Bu sembol uygun Futures sözleşmeleri arasında değil")
    prediction = _active_prediction(symbol, int(time.time()*1000))
    try:
        rows = db.execute("SELECT candle_time, saved_at, signal, confidence, reference, model_version "
                          "FROM signals WHERE symbol=? ORDER BY candle_time DESC LIMIT 40", (symbol,)).fetchall()
    except sqlite3.Error:
        rows = []
    return {"symbol":symbol, "quote": tickers.get(symbol) if tickers.get(symbol) and time.time()-tickers[symbol].get("updated_at",0)<120 else None,
            "prediction":prediction, "unified":combined_assessment(prediction), "history":[{"candle_time":r[0], "saved_at":r[1], "signal":r[2],
            "confidence":r[3], "entry_reference":r[4], "model_version":r[5]} for r in rows]}


@app.get("/api/candles/{symbol}")
async def candles(symbol: str, interval: str="15m", limit: int=Query(100, ge=30, le=500)):
    symbol = symbol.upper()
    if symbol not in universe:
        raise HTTPException(status_code=404, detail="Desteklenmeyen sembol")
    if interval not in ("5m", "15m", "1h", "4h"):
        raise HTTPException(status_code=400, detail="Desteklenmeyen aralık")
    try:
        return {"symbol":symbol, "interval":interval, "candles":await client.klines(symbol, interval, limit)}
    except BinanceError as exc:
        if interval == "15m":
            history=contiguous_recent(db,symbol,needed=30,limit=limit)
            if history:
                return {"symbol":symbol,"interval":interval,"candles":history,"source":"Binance arşiv ve WebSocket (son kayıt; güncellik kontrol edilmeli)","fresh":freshness_ok(history)}
        raise HTTPException(status_code=503, detail=str(exc)) from exc
