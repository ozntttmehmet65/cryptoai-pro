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
from market_stream import ticker_worker, parse_books, BOOK_URL, merge_ordered
from live_candles import (kline_worker, selected_symbols, setup as setup_live,
                          store_bars, contiguous_recent, freshness_ok, seed_recent_rest, history_ready)
from paper_trade import setup as setup_paper, settle as settle_paper, reconcile_quotes, report as historical_paper_report
import execution_paper
import quality_report
from professional import POLICY as PROFESSIONAL_POLICY, HISTORY_TARGET, correlation_groups
from quality_guard import live_drift_block
from execution_guard import guarded_prediction
from execution_policy import POLICY_VERSION as ENTRY_POLICY_VERSION
from order_depth import depth_worker, selected_symbols as depth_symbols, MAX_AGE_SECONDS as DEPTH_MAX_AGE
from ml_engine import ModelEngine
from unified_scoring import combined_assessment
from coin_models import CoinModelRegistry, INFERENCE_HISTORY_BARS
from symbol_utils import symbol_file_key
from price_push import price_delta
from quote_quality import quote_quality, quote_coverage, DISPLAY_QUOTE_AGE_SECONDS

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cryptoai")
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
TRAIN_SYMBOLS = max(3, min(int(os.environ.get("TRAIN_SYMBOLS", "30")), 45))
TRAIN_CANDLES = max(9000, min(int(os.environ.get("PRO_TRAIN_CANDLES", "18000")), HISTORY_TARGET))
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
books: dict[str, dict] = {}
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
inference_lock = asyncio.Lock()
db = sqlite3.connect(str(DATA_DIR / "signals.sqlite"), check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("CREATE TABLE IF NOT EXISTS signals (symbol TEXT, candle_time INTEGER, saved_at INTEGER, signal TEXT, "
           "confidence REAL, p_long REAL, p_short REAL, p_wait REAL, reference REAL, model_version TEXT, "
           "PRIMARY KEY(symbol, candle_time, model_version))")
db.commit()
setup_paper(db)
execution_paper.setup(db)
quality_report.setup(db)
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
    """Use up to 18000 genuine 15m candles; four older pages per due coin.

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
    backfill_exhausted = False
    backfill_retry_at = 0
    try:
        obj = json.loads(f.read_text(encoding="utf-8"))
        if obj.get("symbol") == symbol:
            saved = obj["candles"]
            saved_at = int(obj.get("fetched_at", 0))
            previous_attempt = int(obj.get("older_backfill_attempted_at",0))
            previous_target = int(obj.get("older_backfill_target_count", 3000 if previous_attempt else 0))
            backfill_exhausted = obj.get('older_backfill_exhausted') is True
            backfill_retry_at = float(obj.get('older_backfill_retry_at',0))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    now=time.time()
    age=now-saved_at
    # With a valid cache, a restart must not repeat an enormous REST scan.
    if saved and 0<=age<3*3600 and len(saved)>=TRAIN_CANDLES:
        merged=saved[-TRAIN_CANDLES:]
        cut=len(merged)-1
        while cut>0 and merged[cut]['time']-merged[cut-1]['time']==900000:cut-=1
        return merged[cut:]
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
    if (merged and TRAIN_CANDLES>1500 and len(merged)<TRAIN_CANDLES and now>=backfill_retry_at
            and (not backfill_exhausted or not previous_attempt or now-previous_attempt>=24*3600
                 or previous_target < TRAIN_CANDLES)):
        previous_attempt=int(now)
        previous_target=TRAIN_CANDLES
        backfill_exhausted=False
        for _ in range(4):  # Bounded backfill; continue on a later daily attempt.
            if len(merged)>=TRAIN_CANDLES:
                break
            oldest=int(merged[0]["time"])
            try:
                earlier=await client.klines(symbol, limit=min(1500,TRAIN_CANDLES-len(merged)),
                                            end_time=oldest-1)
            except Exception:
                # Retain successful pages, but don't retry a failed endpoint immediately.
                _atomic_json(f,{'symbol':symbol,'fetched_at':int(now),'candles':merged,
                    'older_backfill_attempted_at':previous_attempt,'older_backfill_target_count':previous_target,
                    'older_backfill_exhausted':False,'older_backfill_retry_at':now+3600})
                raise
            extra={int(b["time"]):b for b in earlier
                   if int(b["close_time"]) < oldest and int(b["time"]) not in clean}
            if not extra:
                backfill_exhausted=True
                break
            clean.update(extra)
            merged=[clean[k] for k in sorted(clean)][-TRAIN_CANDLES:]
            # Preserve each successful page if a later request is interrupted.
            _atomic_json(f,{"symbol":symbol,"fetched_at":int(now),
                            "older_backfill_attempted_at":previous_attempt,
                            "older_backfill_exhausted":False,
                            "older_backfill_retry_at":0,
                            "older_backfill_target_count":previous_target,"candles":merged})
            if len(earlier)<min(1500,TRAIN_CANDLES-len(merged)) and len(extra)<100:
                backfill_exhausted=True
                break
    _atomic_json(f,{"symbol":symbol,"fetched_at":int(now),
                    "older_backfill_attempted_at":previous_attempt,
                    "older_backfill_exhausted":backfill_exhausted,
                    "older_backfill_retry_at":backfill_retry_at,
                    "older_backfill_target_count":previous_target,"candles":merged})
    # Keep only the latest consecutive real segment, never fill a history gap.
    cut=len(merged)-1
    while cut>0 and merged[cut]['time']-merged[cut-1]['time']==900000:cut-=1
    return merged[cut:]


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
    updates = merge_ordered(tickers, updates)
    if not updates:return
    status["tickers_updated"] = max(status["tickers_updated"] or 0, max(q["updated_at"] for q in updates.values()))
    # Evaluate TP/SL at each incoming Binance ticker batch, not on the next
    # 15m candle or a 3-second polling timer. This remains observational only.
    try:
        changed = reconcile_quotes(db, updates, int(time.time()*1000))
        if changed:
            _drift_cache.clear()
            log.info("TP/SL paper results updated: %s",changed)
        now_ms=int(time.time()*1000)
        for symbol, quote in updates.items():
            pred=_active_prediction(symbol,now_ms)
            if pred and pred.get("signal") in ("LONG","SHORT"):
                observe_paper(db,symbol,pred,quote["price"],now_ms)
    except Exception as exc:
        log.warning("TP/SL ticker observation failed: %s",str(exc)[:180])


def _stream_books(updates):
    updates=merge_ordered(books, updates)
    if not updates:return
    _process_books(updates)


depth_books={}


def _market_book(symbol,now_ms):
    top=books.get(symbol)
    depth=depth_books.get(symbol)
    if depth and 0<=now_ms/1000-depth.get('updated_at',0)<DEPTH_MAX_AGE and (not top or depth['updated_at']>=top.get('updated_at',0)):
        return depth
    return top


def _depth_symbols():
    opened=[r[0] for r in db.execute("SELECT DISTINCT symbol FROM executable_paper WHERE state='OPEN' AND shadow=0 ORDER BY symbol").fetchall()]
    return depth_symbols(universe,tickers,signals,opened)


def _stream_depth(updates):
    updates=merge_ordered(depth_books,updates)
    if updates:_process_books(updates)


def _process_books(updates):
    try:
        now_ms=int(time.time()*1000)
        selected={symbol:_market_book(symbol,now_ms) for symbol in updates}
        changed=execution_paper.reconcile_books(db,selected,now_ms)
        if changed:_drift_cache.clear();_quality_cache.clear()
        for symbol in updates:
            pred=_active_prediction(symbol,now_ms)
            if pred and pred.get('signal') in ('LONG','SHORT'):
                observe_paper(db,symbol,pred,None,now_ms)
    except Exception as exc:
        log.warning('Bid/ask paper observation failed: %s',str(exc)[:180])

def _book_status(value):
    status["book_source"] = "WebSocket: " + value

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
    local_seeded = set()
    while True:
        symbols = _live_symbols()
        ready = 0
        for idx, symbol in enumerate(symbols):
            if symbol not in local_seeded:
                local_seeded.add(symbol)
                try:
                    cached = json.loads((TRAINING_CACHE / (symbol_file_key(symbol) + '.json')).read_text(encoding='utf-8'))
                    if cached.get('symbol') == symbol:
                        store_bars(db, symbol, cached.get('candles', [])[-INFERENCE_HISTORY_BARS:])
                except (OSError, ValueError, TypeError, KeyError):
                    pass
            candles = contiguous_recent(db, symbol, INFERENCE_HISTORY_BARS, limit=INFERENCE_HISTORY_BARS)
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
                    inserted = await seed_recent_rest(client, db, symbol, limit=INFERENCE_HISTORY_BARS+1)
                    if inserted:
                        status["rest_bootstrap_count"] += 1
                    if freshness_ok(contiguous_recent(db, symbol, INFERENCE_HISTORY_BARS, limit=INFERENCE_HISTORY_BARS)):
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
                if freshness_ok(contiguous_recent(db,symbol,INFERENCE_HISTORY_BARS,limit=INFERENCE_HISTORY_BARS)):
                    ready += 1
                    status["daily_history_ready"] = ready
                    enqueue_inference(symbol, "Binance resmî arşiv kapanmış 15dk mumlar")
            if idx % 16 == 0:
                status["daily_history_ready"] = ready
            await asyncio.sleep(0.03)
        status["daily_history_ready"] = sum(1 for sym in symbols
                if freshness_ok(contiguous_recent(db,sym,INFERENCE_HISTORY_BARS,limit=INFERENCE_HISTORY_BARS)))
        await asyncio.sleep(180)



_drift_cache = {}
_quality_cache = {}


def observe_paper(db,symbol,pred,observed_price,now_ms,*,shadow=False):
    created=execution_paper.observe(db,symbol,pred,_market_book(symbol,now_ms),now_ms,shadow=shadow)
    if created:
        quality_report.record(db,symbol,pred,now_ms)
        _quality_cache.clear()
    return created


def paper_report(db,limit=100):
    return execution_paper.report(db,limit,historical=historical_paper_report(db,limit))

def _current_drift(side, now_ms, symbol=None, version=None):
    key=(side,symbol,version)
    cached = _drift_cache.get(key)
    if cached and 0 <= now_ms - cached[0] < 1000:
        return cached[1]
    value = live_drift_block(db, side, now_ms, symbol, version)[0]
    _drift_cache[key] = (now_ms, value)
    return value


def _guard(symbol, pred, now_ms):
    side = pred.get('candidate_signal') if pred.get('signal')=='BEKLE' else pred.get('signal')
    permission = coin_engine.permissions(symbol).get(side, {}).get('approved', False)
    return guarded_prediction(pred, tickers.get(symbol), now_ms, permission,
                              (_current_drift(side, now_ms) or _current_drift(side, now_ms, symbol, pred.get('model_version'))) if side in ('LONG', 'SHORT') else False, book=_market_book(symbol,now_ms))


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
    quality_report.record(db,symbol,inf,now_ms)
    _quality_cache.clear()
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


async def infer_async(symbol, candles):
    # Serialize model inference, but never run pandas/sklearn on the network loop.
    # SQLite and signal publication remain on the event-loop thread.
    async with inference_lock:
        return await asyncio.to_thread(coin_engine.infer, symbol, candles)


async def stream_signal_worker():
    """REST-block independent inference on consecutive streamed/archive candles."""
    while True:
        item = await closed_queue.get()
        symbol, source = item if isinstance(item, tuple) else (item, "WebSocket kapanmış 15dk")
        try:
            if symbol not in universe or not coin_engine.ready(symbol):
                continue
            candles=contiguous_recent(db,symbol,INFERENCE_HISTORY_BARS,limit=INFERENCE_HISTORY_BARS)
            if not freshness_ok(candles):continue
            result=await infer_async(symbol,candles)
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
        finally:
            closed_queue.task_done()
            # A restored/all-market queue must not monopolize the event loop:
            # let WebSocket handshakes, quotes and HTTP run between predictions.
            await asyncio.sleep(0)


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
            bars = await client.klines(symbol, limit=INFERENCE_HISTORY_BARS+1)
            now_ms = int(time.time()*1000)
            settle_paper(db, symbol, bars, now_ms)
            inf = await infer_async(symbol,bars)
            now_ms = int(time.time()*1000)
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
        # A V7.2 professional artifact still needs the V7.3 RSI/policy repair.
        # Prioritize all outstanding pipeline migrations by current volume;
        # failed/new-listing histories retain their existing retry backoff.
        symbols = sorted(universe, key=lambda s: (coin_engine.pipeline_ready(s),
                         -tickers.get(s,{}).get("quote_volume",0), s))
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
                status["training_stage"] = f"Binance dinlenme süresi · {client.cooldown_seconds} sn; oylama modelleri bekleniyor"
                await asyncio.sleep(min(900,client.cooldown_seconds+1))
                break  # Do not wait once PER COIN during a shared Binance ban.
            last_attempt[symbol] = now
            status["coin_training_current"] = symbol
            status["model_training"] = True
            status["training_stage"] = f"Profesyonel dönem ve oylama eğitimi: {symbol}"
            try:
                bars = await _training_bars(symbol)
                store_bars(db, symbol, bars[-INFERENCE_HISTORY_BARS:])
                info = await asyncio.to_thread(coin_engine.fit, symbol, bars)
                status["coin_training_success"] += 1
                status["coin_training_last"] = {"symbol":symbol,"version":info["version"],
                                                 "at":info["trained_at"],"promotion":info.get('promotion'),
                                                 "history_count":info.get('historical_candles_used')}
                status["coin_last_failure"] = None
                failures.pop(symbol,None)
                status["coin_failure_reasons"].pop(symbol, None)
                # Training is independent of the 15-minute inference queue;
                # model is available immediately for the last CLOSED candle.
                if freshness_ok(contiguous_recent(db,symbol,INFERENCE_HISTORY_BARS,limit=INFERENCE_HISTORY_BARS)):
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


async def funding_worker():
    """Only positions needing certified funding coverage use shared REST pacing."""
    while True:
        try:
            if not client.cooldown_seconds:
                now_ms=int(time.time()*1000)
                for symbol,start,end in execution_paper.funding_jobs(db,now_ms):
                    db.execute("UPDATE executable_paper SET funding_requested_at=? WHERE symbol=? AND state IN ('OPEN','AWAITING_FUNDING')",(now_ms,symbol));db.commit()
                    events=await client.get('/fapi/v1/fundingRate',symbol=symbol,startTime=start,endTime=end,limit=1000)
                    complete=execution_paper.apply_funding(db,symbol,start,end,events,int(time.time()*1000))
                    status['funding_last_success']=utc_now() if complete else status.get('funding_last_success')
                    status['funding_last_error']=None if complete else 'Funding yanıtı eksik veya geçersiz; net sonuç bekletiliyor'
                    _drift_cache.clear();_quality_cache.clear()
        except Exception as exc:
            status['funding_last_error']=str(exc)[:180]
            log.warning('Funding coverage postponed: %s',str(exc)[:180])
        await asyncio.sleep(15)


async def quality_worker():
    while True:
        try:quality_report.snapshot(db,int(time.time()*1000))
        except Exception as exc:log.warning('Daily quality snapshot failed: %s',str(exc)[:160])
        await asyncio.sleep(300)


def _professional_report(now_ms):
    cached=_quality_cache.get('report')
    if cached and 0<=now_ms-cached[0]<20000:return cached[1]
    value=quality_report.report(db,now_ms)
    eligible=[]
    for symbol in signals:
        pred=_active_prediction(symbol,now_ms)
        if not pred:continue
        combined=combined_assessment(pred)
        if combined.get('direction') in ('LONG','SHORT') and combined.get('status') in ('İŞLEM SİNYALİ','TAKİP ET'):
            eligible.append((symbol,combined['direction'],combined.get('score',0)))
    eligible.sort(key=lambda r:-r[2]);eligible=eligible[:60]
    histories={s:contiguous_recent(db,s,needed=80,limit=100) for s,_,_ in eligible}
    value['risk_groups']=correlation_groups(histories,{s:side for s,side,_ in eligible})
    value['models']=coin_engine.stats(universe)
    value['funding']={'last_success':status.get('funding_last_success'),'last_error':status.get('funding_last_error'),
        'pending_results':db.execute("SELECT COUNT(*) FROM executable_paper WHERE shadow=0 AND state='AWAITING_FUNDING'").fetchone()[0]}
    value['policy']=PROFESSIONAL_POLICY
    value['release']='V7.2'
    value['entry_policy']=ENTRY_POLICY_VERSION
    _quality_cache['report']=(now_ms,value);return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_snapshot()
    status["training_stage"] = (f"7/24 coin bazli egitim — "
                                 f"{coin_engine.stats(universe)['trained']} ayrik model diskten bulundu")
    tasks = [asyncio.create_task(market_worker()),
             asyncio.create_task(coin_training_worker()),
             asyncio.create_task(ticker_worker(lambda: set(universe), _stream_prices, _stream_status)),
             asyncio.create_task(ticker_worker(lambda: set(universe), _stream_books, _book_status, url=BOOK_URL, parser=parse_books)),
             asyncio.create_task(depth_worker(_depth_symbols,_stream_depth,lambda value:status.update(depth_source='WebSocket: '+value))),
             asyncio.create_task(kline_worker(_live_symbols, _on_kline_close, _kline_state)),
             asyncio.create_task(history_warm_worker()),
             asyncio.create_task(stream_signal_worker()),
             asyncio.create_task(paper_settlement_worker()),
             asyncio.create_task(funding_worker()),
             asyncio.create_task(quality_worker())]
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


def _execution_monitor(now_ms):
    from collections import Counter
    fresh_books=sum(1 for sym in universe if sym in books and 0<=now_ms/1000-books[sym].get('updated_at',0)<12)
    counts=Counter(); candidates=approved=0
    for symbol in signals:
        pred=_active_prediction(symbol,now_ms)
        if not pred or pred.get('candidate_signal') not in ('LONG','SHORT'):continue
        candidates+=1
        audit=pred.get('execution_quality') or {}
        if pred.get('signal') in ('LONG','SHORT') and audit.get('approved'):approved+=1
        counts.update(set(audit.get('reasons') or []))
    return {'policy_version':ENTRY_POLICY_VERSION,'book_stream':status.get('book_source','Bekleniyor'),
            'depth_stream':status.get('depth_source','Bekleniyor'),
            'depth_watched':len(_depth_symbols()),
            'fresh_depth_books':sum(1 for s,d in depth_books.items() if s in universe and 0<=now_ms/1000-d.get('updated_at',0)<DEPTH_MAX_AGE),
            'fresh_books':fresh_books,'total_symbols':len(universe),'active_candidates':candidates,
            'entry_passed':approved,'veto_reasons':[{'reason':r,'count':n} for r,n in counts.most_common(8)],
            'notice':'Anlık giriş kontrolü; sebepler aynı adayda birlikte bulunabilir. Üst kademe verisi tüm derinliği veya emir gerçekleşmesini kanıtlamaz.'}


@app.get('/api/execution-monitor')
async def execution_monitor():
    return _execution_monitor(int(time.time()*1000))


@app.get('/api/professional-report')
async def professional_report():
    return _professional_report(int(time.time()*1000))


def _signal_diagnostics(now_ms):
    from collections import Counter
    failures = {side: Counter() for side in ('LONG', 'SHORT')}
    labels = {}
    stages = Counter()
    model_causes=Counter()
    for symbol in universe:
        if not coin_engine.ready(symbol):
            stages['model_pending'] += 1
            continue
        for side, permission in coin_engine.permissions(symbol).items():
            info=coin_engine.info(symbol) or {}
            direction=(info.get('directional_validation') or {}).get(side) or {}
            policy=info.get('decision_policy')
            choices=policy.get('candidates',[]) if isinstance(policy,dict) else []
            validation_rows=[r['directions'].get(side,{}) for r in choices
                             if isinstance(r,dict) and isinstance(r.get('directions'),dict)]
            model_causes['directions_checked']+=1
            if permission['approved']:model_causes['directions_approved']+=1
            if direction.get('calibration_alpha')==0:model_causes['baseline_only']+=1
            if validation_rows and all(r.get('signals',0)==0 for r in validation_rows):model_causes['validation_no_candidates']+=1
            elif validation_rows and all(r.get('mean_stressed_net_return') is None or r['mean_stressed_net_return']<=0 for r in validation_rows):model_causes['validation_no_positive_net']+=1
            if direction.get('signals',0)==0:model_causes['holdout_no_candidates']+=1
            if info.get('historical_candles_used',0)<TRAIN_CANDLES:model_causes['history_below_target']+=1
            for row in permission.get('checks', []):
                if not row['passed']:
                    failures[side][row['id']] += 1
                    labels[row['id']] = row['label']
        pred = signals.get(symbol)
        if not pred or pred.get('model_version') != coin_engine.version(symbol):
            stages['inference_pending' if history_ready(db, symbol, INFERENCE_HISTORY_BARS, now_ms) else 'history_pending'] += 1
        elif not 0 <= now_ms-pred.get('candle_time', 0)-900000 < 900000:
            stages['entry_window_expired'] += 1
        elif pred.get('candidate_signal') not in ('LONG', 'SHORT'):
            stages['score_or_direction_margin'] += 1
        else:
            stages['active_candidates'] += 1
            active = _active_prediction(symbol, now_ms)
            if active and active.get('signal') in ('LONG', 'SHORT'):
                stages['entry_approved'] += 1
    return {'as_of': now_ms, 'stages': dict(stages),
            'model_causes':dict(model_causes),
            'required_inference_bars': INFERENCE_HISTORY_BARS,
            'model_rejections': {side: [{'id': key, 'reason': labels[key], 'count': count}
                                      for key, count in counter.most_common()]
                                 for side, counter in failures.items()},
            'threshold_policy': 'Coin başına doğrulamada seçilir; ayrı testten geçmeden işlem onayı verilmez',
            'notice': 'Bir yön birden fazla şartta kalabilir; model sayısı onaylı sinyal sayısı değildir.'}


@app.get('/api/signal-diagnostics')
async def signal_diagnostics():
    return _signal_diagnostics(int(time.time()*1000))


@app.get("/api/health")
async def health():
    now = time.time()
    fresh = status["tickers_updated"] is not None and now - status["tickers_updated"] < 120
    return {"server_time":int(now*1000), "quote_freshness_version":"V7.3.1", "release":ENTRY_POLICY_VERSION,
            "provider": "Binance USDⓈ-M Futures (WebSocket tickers + REST candles + official archive)", "provider_ok": fresh,
            "model_ready": coin_engine.stats(universe)["professional_trained"] > 0,
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
            "quote_coverage": quote_coverage(universe, tickers, now),
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
            "unified_scoring_version":"V7.2",
            "signal_repair_version":"V7.3",
            "signal_diagnostics": _signal_diagnostics(int(now*1000)),
            "execution_monitor": _execution_monitor(int(now*1000)),
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
    now = time.time()
    fresh = status["tickers_updated"] is not None and time.time()-status["tickers_updated"] < 120
    rows = []
    watching = set(_live_symbols())
    for sym, entry in universe.items():
        if search and search.upper() not in sym:
            continue
        quote_candidate=tickers.get(sym, {})
        quality = quote_quality(quote_candidate, now)
        quote = quote_candidate if quality['age_seconds'] is not None and 0 <= quality['age_seconds'] < DISPLAY_QUOTE_AGE_SECONDS else {}
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
               "quote_quality":quality,
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
    return {"total":len(rows), "rows":rows[:limit], "fresh":fresh, "server_time":int(now*1000),
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
    return {'version':'V7.2','total':len(rows),'counts':counts,'rows':rows[:limit],
            'notice':'AI + indikatör + formasyon + 1s/4s tek puan ve tek yön. TAKİP ET doğrulanmış işlem sinyali değildir. 0-100 puan TP başarı yüzdesi değildir.'}


@app.get("/api/paper")
async def paper(limit: int = Query(100, ge=1, le=500)):
    """Forward paper observations only, never retrofitted historical trades."""
    return paper_report(db,limit)


@app.get("/api/model")
async def model_info():
    return {"ready": coin_engine.stats(universe)["professional_trained"]>0,
            "training":status["model_training"], "stage":status["training_stage"],
            "scope":"independent_model_per_symbol",
            "coin_training":coin_engine.stats(universe),
            "legacy_global_model":{"exists":engine.model is not None,
                                  "version":(engine.info or {}).get("version"),
                                  "used_for_live_signals":False},
            "notice":"V7.3: Üç algoritmalı oy, en az 9.000 mum, 24s ayrılmış en az 12 sonuç, üç kronolojik dönem ve 1.400 yön testi için ihtiyatlı çoklu seçim taraması. Piyasa koşulu kanıtı ve canlı giriş denetimi gerekir. Aday model mevcut onaylı yönleri koruma şartıyla kabul edilir; farklı dönemlerin puanları sıralanmaz. Alış/satış + ücret/kayma varsayımı + gerçekleşmiş funding ile sanal takip; gerçek emir yok."}


@app.post("/api/admin/retrain")
async def retrain(x_admin_token: str | None = Header(default=None)):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Yönetici anahtarı zorunlu")
    status["coin_force_refresh"] = True
    return {"message": "563 coin icin sirali yeni egitim turu istendi; 7/24 gorev sonraki turda uygulayacak"}


@app.post('/api/admin/rollback/{symbol}')
async def rollback(symbol:str,version:str,x_admin_token:str|None=Header(default=None)):
    if not ADMIN_TOKEN or x_admin_token!=ADMIN_TOKEN:
        raise HTTPException(status_code=403,detail='Yönetici anahtarı zorunlu')
    symbol=symbol.upper()
    if symbol not in universe:raise HTTPException(status_code=404,detail='Desteklenmeyen sözleşme')
    if status.get('coin_training_current')==symbol:
        raise HTTPException(status_code=409,detail='Bu coinin eğitimi devam ediyor; sürüm değişimi bekletildi')
    try:info=coin_engine.restore_version(symbol,version)
    except (ValueError,OSError,KeyError) as exc:raise HTTPException(status_code=400,detail=str(exc)[:180]) from exc
    signals.pop(symbol,None);_drift_cache.clear();_quality_cache.clear()
    enqueue_inference(symbol,'Doğrulanmış arşiv sürümüne geri dönüş')
    return {'symbol':symbol,'active_version':info['version'],'notice':'Giriş, piyasa koşulu ve canlı sonuç kilitleri yeniden uygulanır'}


@app.get("/api/coin-model/{symbol}")
async def coin_model_detail(symbol: str):
    symbol = symbol.upper()
    if symbol not in universe:
        raise HTTPException(status_code=404,detail="Desteklenmeyen Futures sozlesmesi")
    return {"symbol":symbol, "trained":coin_engine.ready(symbol),
            "ensemble_ready":coin_engine.ensemble_ready(symbol),
            "professional_ready":coin_engine.professional_ready(symbol),
            "latest_candidate":coin_engine.challengers.get(symbol),
            "versions":coin_engine.version_history(symbol),
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
                          "ensemble_ready":coin_engine.ensemble_ready(sym),
                          "professional_ready":coin_engine.professional_ready(sym),
                          "history_count":info.get('historical_candles_used'),
                          "test_samples":info.get("test_samples"),
                          "long_failed_checks":p["LONG"].get("failed_checks", []),
                          "short_failed_checks":p["SHORT"].get("failed_checks", []),
                          "decision_threshold":(info.get("decision_policy") or {}).get("threshold", .65),
                          "direction_thresholds":(info.get('decision_policy') or {}).get('thresholds'),
                          "training_pipeline_version":info.get('training_pipeline_version')})
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
            "directional_holdout":(coin_engine.info(symbol) or {}).get("directional_validation",{}),
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
