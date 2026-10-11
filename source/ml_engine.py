"""Real trained ML classification, chronological/purged split and held-out evaluation.
No indicator threshold is substituted for model outputs.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import accuracy_score, f1_score
from quality_guard import model_permissions, tp_sl_direction_metrics
from technical_analysis import EXTRA_COLS, enrich

LOG = logging.getLogger(__name__)
INTERVAL_MS = 15 * 60 * 1000
# Lookahead bounds only the training/holdout label search. It does NOT close
# live virtual positions: those remain open until a tracked TP/SL first touch.
EVALUATION_BARS = 96    # 24-hour observable first-touch window for holdout labels
EVALUATION_METHOD = "tp_sl_first_touch_24h_v1"
# Estimate: two-sided taker commissions ~ 0.10% + slippage allowance ~0.06%; variable by user/exchange
ROUND_TRIP_COST = 0.0016
LABEL_MOVE_THRESHOLD = ROUND_TRIP_COST + 0.0006
SIGNAL_THRESHOLD = 0.65
COLS = ["r1", "r3", "r6", "r12", "vol12", "vol24", "trend8", "trend21",
        "range", "body", "rsi14", "volume_z", "volume_change", "atr_pct", "high_break", "low_break"] + EXTRA_COLS
CLASSES = (-1, 0, 1)


def first_touch_label(price: float, atr_pct: float, future_high, future_low):
    """Determine first TP/SL touch separately for LONG and SHORT from OHLC bars.

    Returns {1: LONG, -1: SHORT}, with +1=TP first, -1=SL first,
    0=neither observed inside the *backtest lookahead* and 2=ambiguous same-bar
    TP and SL touch. Ambiguous data are never treated as a win.
    """
    if not np.isfinite(price) or not np.isfinite(atr_pct) or price <= 0 or atr_pct <= 0:
        return {1:0,-1:0}
    ltp=price*(1+2.5*atr_pct)
    lsl=price*(1-1.5*atr_pct)
    stp=price*(1-2.5*atr_pct)
    ssl=price*(1+1.5*atr_pct)
    if min(ltp,lsl,stp,ssl) <= 0:
        return {1:0,-1:0}
    highs=np.asarray(future_high,dtype=float)
    lows=np.asarray(future_low,dtype=float)
    def _first(tp_hits,sl_hits):
        tidx=np.flatnonzero(tp_hits)
        sidx=np.flatnonzero(sl_hits)
        if not len(tidx) and not len(sidx):return 0
        if not len(tidx):return -1
        if not len(sidx):return 1
        if tidx[0]==sidx[0]:return 2  # Intrabar order unknown, not a win
        return 1 if tidx[0]<sidx[0] else -1
    return {1:_first(highs>=ltp,lows<=lsl),
            -1:_first(lows<=stp,highs>=ssl)}


def features(candles: list[dict], training: bool = False) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles).sort_values("time").drop_duplicates("time").copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    close = df["close"]
    safe_close = close.replace(0, np.nan)
    ret = safe_close.pct_change(fill_method=None)
    df["r1"] = ret
    df["r3"] = safe_close.pct_change(3, fill_method=None)
    df["r6"] = safe_close.pct_change(6, fill_method=None)
    df["r12"] = safe_close.pct_change(12, fill_method=None)
    df["vol12"] = ret.rolling(12).std()
    df["vol24"] = ret.rolling(24).std()
    df["trend8"] = close / close.ewm(span=8, adjust=False).mean() - 1
    df["trend21"] = close / close.ewm(span=21, adjust=False).mean() - 1
    df["range"] = (df["high"] - df["low"]) / safe_close
    df["body"] = (df["close"] - df["open"]) / safe_close
    delta = close.diff()
    ups = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    downs = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    # Zero losses imply RSI=100; flat prices imply neutral RSI=50.
    df["rsi14"] = (ups / (ups + downs).replace(0, np.nan)).fillna(.5).clip(0, 1)
    vol = np.log1p(df["volume"].clip(lower=0))
    df["volume_z"] = (vol - vol.rolling(24).mean()) / vol.rolling(24).std().replace(0, np.nan)
    df["volume_change"] = vol.diff()
    tr = pd.concat([df["high"]-df["low"], (df["high"]-close.shift()).abs(),
                    (df["low"]-close.shift()).abs()], axis=1).max(axis=1)
    df["atr_pct"] = tr.rolling(14).mean() / safe_close
    df["high_break"] = close / df["high"].shift(1).rolling(20).max() - 1
    df["low_break"] = close / df["low"].shift(1).rolling(20).min() - 1
    enrich(df)
    if training:
        # Predict relative *first* ATR-bracket TP/SL hits, not the return 45
        # minutes later. Rows without a TP/SL hit inside the LOOKAHEAD are
        # labelled WAIT but NO hypothetical trade is closed at that time.
        hi=df['high'].to_numpy(dtype=float)
        lo=df['low'].to_numpy(dtype=float)
        cl=df['close'].to_numpy(dtype=float)
        atr=df['atr_pct'].to_numpy(dtype=float)
        time_arr=df['time'].to_numpy(dtype=np.int64)
        n=len(df)
        long_result=np.zeros(n,dtype=np.int8)
        short_result=np.zeros(n,dtype=np.int8)
        label=np.zeros(n,dtype=np.int8)
        # Reject samples whose evaluation window contains absent 15m bars.
        gap=np.concatenate(([0],np.cumsum(np.diff(time_arr)!=INTERVAL_MS)))
        for i in range(max(0,n-EVALUATION_BARS)):
            if gap[i+EVALUATION_BARS]!=gap[i]:
                long_result[i]=2;short_result[i]=2
                continue
            outcomes=first_touch_label(cl[i],atr[i],
                 hi[i+1:i+1+EVALUATION_BARS],lo[i+1:i+1+EVALUATION_BARS])
            long_result[i]=outcomes[1]
            short_result[i]=outcomes[-1]
            if outcomes[1]==1 and outcomes[-1]==-1:label[i]=1
            elif outcomes[-1]==1 and outcomes[1]==-1:label[i]=-1
        df['label']=label
        df['long_outcome']=long_result
        df['short_outcome']=short_result
        df=df.iloc[:-EVALUATION_BARS].copy()
        df=df.replace([np.inf,-np.inf],np.nan).dropna(subset=COLS)
        df=df[(df['long_outcome']!=2)&(df['short_outcome']!=2)]
    else:
        df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=COLS)
    return df


class ModelEngine:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "model.joblib"
        self.meta_path = Path(data_dir) / "model-info.json"
        self.model = None
        self.calibrators = {}
        self.info = None
        self.load()

    def load(self):
        if self.path.exists() and self.meta_path.exists():
            try:
                artifact = joblib.load(self.path)
                self.model = artifact["model"]
                self.calibrators = artifact["calibrators"]
                self.info = json.loads(self.meta_path.read_text(encoding="utf-8"))
                LOG.info("Model loaded: %s", self.info.get("version"))
            except Exception:
                LOG.exception("Persisted model load failed")
                self.model = None

    def probabilities(self, X) -> dict[int, np.ndarray]:
        if self.model is None:
            raise RuntimeError("Model henüz eğitilmedi")
        raw = self.model.predict_proba(X)
        by_class = {int(k): raw[:, j] for j, k in enumerate(self.model.classes_)}
        p = np.stack([self.calibrators[k].predict(by_class.get(k, np.zeros(len(X))))
                      for k in CLASSES], axis=1)
        p = np.clip(p, 0.0001, 1.0)
        p = p / p.sum(axis=1, keepdims=True)
        return {k: p[:, i] for i, k in enumerate(CLASSES)}

    @staticmethod
    def decision(p: dict[int, float]):
        if p[1] >= SIGNAL_THRESHOLD and p[1] >= p[-1] + 0.1:
            return "LONG", p[1]
        if p[-1] >= SIGNAL_THRESHOLD and p[-1] >= p[1] + 0.1:
            return "SHORT", p[-1]
        return "BEKLE", max(p.values())

    def permissions(self):
        return model_permissions(self.info)

    def infer(self, candles: list[dict]):
        if self.model is None:
            return None
        # Fail closed on missing or stale intervals, regardless of upstream source.
        # This applies to REST *and* WebSocket to prevent stale model decisions.
        from time import time
        now_ms = int(time()*1000)
        closed = sorted((c for c in candles if c["close_time"] < now_ms), key=lambda c:c["time"])
        if len(closed) < 80 or now_ms - closed[-1]["close_time"] > 35*60*1000:
            return None
        if any(closed[i]["time"] - closed[i-1]["time"] != INTERVAL_MS
               for i in range(len(closed)-79,len(closed))):
            return None
        frame = features(closed[-180:])
        if len(frame) == 0:
            return None
        last = frame.iloc[-1]
        X = last[COLS].to_numpy(dtype=float).reshape(1, -1)
        predicted = self.probabilities(X)
        p = {k: float(v[0]) for k, v in predicted.items()}
        decision, conf = self.decision(p)
        candidate_signal = decision if decision in ("LONG", "SHORT") else None
        blocked_reason = None
        if decision in ("LONG", "SHORT") and not self.permissions()[decision]["approved"]:
            blocked_reason = "Yön bağımsız testle doğrulanmadı; güvenlik kilidi aktif"
            decision = "BEKLE"
            conf = p[0]  # NEVER present the rejected LONG/SHORT probability as BEKLE confidence.
        entry = float(last["close"])
        atr = float(last["atr_pct"]*entry)
        stop = target = None
        if decision == "LONG":
            stop, target = entry - 1.5*atr, entry + 2.5*atr
        if decision == "SHORT":
            stop, target = entry + 1.5*atr, entry - 2.5*atr
        return {"signal": decision, "confidence": round(conf, 4),
                "p_long": round(p[1], 4), "p_short": round(p[-1], 4), "p_wait": round(p[0], 4),
                "blocked_reason": blocked_reason, "candidate_signal": candidate_signal,
                "entry_reference": entry, "stop_estimate": stop, "target_estimate": target,
                "risk_atr_pct": float(last["atr_pct"]),
                "candle_time": int(last["time"]), "model_version": self.info["version"],
                "timeframe": "15m", "horizon_minutes": None,
                "risk_exit_method": "tp_sl_first_touch_no_expiry"}

    def fit(self, per_symbol: dict[str, list[dict]]):
        frames = []
        for symbol, candles in per_symbol.items():
            frame = features(candles, training=True)
            if len(frame) > 100:
                frame["symbol"] = symbol
                frames.append(frame)
        if len(frames) < 3:
            raise ValueError("Model eğitimi için en az 3 sembolde yeterli geçmiş veri olmalı")
        whole = pd.concat(frames, ignore_index=True).sort_values("time")
        times = np.sort(whole["time"].unique())
        if len(times) < 250:
            raise ValueError("Kronolojik ayrım için yeterli benzersiz mum yok")
        val_start = times[int(len(times)*.65)]
        test_start = times[int(len(times)*.80)]
        purge_ms = (EVALUATION_BARS + 1) * INTERVAL_MS
        train = whole[whole["time"] < val_start-purge_ms]
        valid = whole[(whole["time"] >= val_start) & (whole["time"] < test_start-purge_ms)]
        test = whole[whole["time"] >= test_start]
        if min(len(train), len(valid), len(test)) < 150:
            raise ValueError("Eğitim/doğrulama/test bölümlerinden biri çok küçük")
        if len(set(train["label"])) < 3:
            raise ValueError("Eğitim verisinde 3 sınıf da gözlenmedi")
        model = HistGradientBoostingClassifier(max_iter=105, max_leaf_nodes=15,
                                                 l2_regularization=1.0, learning_rate=.065,
                                                 min_samples_leaf=35, random_state=42)
        model.fit(train[COLS].to_numpy(), train["label"].to_numpy())
        vp = model.predict_proba(valid[COLS].to_numpy())
        cals = {}
        for cls in CLASSES:
            idx = list(model.classes_).index(cls)
            cals[cls] = IsotonicRegression(out_of_bounds="clip", y_min=.0001, y_max=1.)
            cals[cls].fit(vp[:, idx], (valid["label"].to_numpy()==cls).astype(int))
        tp = model.predict_proba(test[COLS].to_numpy())
        calibrated = np.stack([cals[cls].predict(tp[:, list(model.classes_).index(cls)])
                               for cls in CLASSES], axis=1)
        calibrated = np.clip(calibrated, .0001, 1)
        calibrated /= calibrated.sum(axis=1, keepdims=True)
        predicted = np.array(CLASSES)[calibrated.argmax(axis=1)]
        actions = []
        for ps in calibrated:
            d, _ = self.decision({cls: float(ps[i]) for i, cls in enumerate(CLASSES)})
            actions.append(1 if d == "LONG" else -1 if d == "SHORT" else 0)
        actions = np.array(actions)
        # Score the holdout by *which ATR barrier was touched first*.
        # Unresolved events are not profitable exits; the audit explicitly
        # reports their fraction. No fixed-time synthetic close is credited.
        tp_results=test[['long_outcome','short_outcome']].to_numpy(dtype=int)
        atrs=test['atr_pct'].to_numpy(dtype=float)
        validation_times=test['time'].to_numpy(dtype=np.int64)
        direction_validation = {
            'LONG': tp_sl_direction_metrics(actions,tp_results[:,0],atrs,
                                             validation_times,1),
            'SHORT': tp_sl_direction_metrics(actions,tp_results[:,1],atrs,
                                              validation_times,-1),
        }
        taken = actions != 0
        validated_returns=[]
        hit_n=0
        for action, outcomes, atr in zip(actions,tp_results,atrs):
            if not action:continue
            touched=outcomes[0] if action==1 else outcomes[1]
            if touched==1:
                hit_n+=1
                validated_returns.append(2.5*atr-ROUND_TRIP_COST)
            elif touched==-1:
                validated_returns.append(-1.5*atr-ROUND_TRIP_COST)
        real_outcomes_n=len(validated_returns)
        metrics = {
            "test_accuracy": round(float(accuracy_score(test["label"], predicted)), 4),
            "test_macro_f1": round(float(f1_score(test["label"], predicted, average="macro", zero_division=0)), 4),
            "test_signals": int(taken.sum()),
            "test_signal_direction_accuracy": round(hit_n/real_outcomes_n,4) if real_outcomes_n else None,
            "test_mean_net_return_per_signal": round(float(np.mean(validated_returns)), 6) if real_outcomes_n else None,
            "test_resolved_tp_sl": real_outcomes_n,
            "test_unresolved_tp_sl": int(taken.sum())-real_outcomes_n,
            "assumed_roundtrip_fee_slippage": ROUND_TRIP_COST,
            "note": "Test 24 saatlik doğrulama penceresinde TP/SL ilk temasına dayanır; bu süre canlı pozisyonu kapatmaz. OHLC aynı mumda iki seviyeye değerse sonuç belirsiz sayılır. Değişken kayma, funding, emir dolumu ve likidasyon hariçtir."
        }
        version = datetime.now(timezone.utc).strftime("ml-%Y%m%dT%H%M%SZ")
        info = {"version": version, "algorithm": "HistGradientBoostingClassifier + isotonic calibration",
                "trained_at": datetime.now(timezone.utc).isoformat(), "training_symbols": list(per_symbol),
                "training_sample_count": len(train), "validation_sample_count": len(valid),
                "test_sample_count": len(test), "data_start": datetime.fromtimestamp(int(times[0])/1000, tz=timezone.utc).isoformat(),
                "data_end": datetime.fromtimestamp(int(times[-1])/1000, tz=timezone.utc).isoformat(),
                "timeframe": "15m", "prediction_horizon_minutes": None,
                "evaluation_lookahead_hours": EVALUATION_BARS//4,
                "evaluation_method": EVALUATION_METHOD,
                "trade_exit_method": "TP_SL_FIRST_TOUCH_NO_TIME_LIMIT",
                "label_threshold": None, "decision_threshold": SIGNAL_THRESHOLD,
                "metrics": metrics, "directional_validation": direction_validation,
                "limits": "TP/SL geçmiş testte yalnızca en çok 24s gözlem penceresinde değerlendirildi; bu canlı takipte zaman aşımı ile pozisyon kapatma anlamına gelmez. Tek ortak modeldir. Canlı ticker son işlem fiyatı emir dolumu ve tasfiye verisi değildir."}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        import os
        tmp_path = self.path.parent / "model.temp.joblib"
        joblib.dump({"model": model, "calibrators": cals}, tmp_path)
        os.replace(tmp_path, self.path)
        self.meta_path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        self.model = model
        self.calibrators = cals
        self.info = info
        return info
