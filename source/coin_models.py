"""Persistent, independently validated per-contract models.

All training is single-file/single-CPU, sequentially scheduled by app.py. Each
symbol has its own model, chronological holdout metrics and promotion decision.
No global-model directional fallback, no fabricated price labels, no trade orders.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import os
import time

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, f1_score

from ml_engine import (features, COLS, CLASSES, INTERVAL_MS,
                       EVALUATION_BARS, ROUND_TRIP_COST,
                       SIGNAL_THRESHOLD)
from quality_guard import model_permissions, tp_sl_direction_metrics
from technical_analysis import setup_arrays, technical_scan
from risk_plan import choose_on_train, entry_guidance, FALLBACK, ASSUMED_STRESS_COST
from symbol_utils import symbol_valid, symbol_file_key

LOG = logging.getLogger(__name__)
MODEL_FAMILY = "tp-sl-adaptive-brackets-per-symbol-v60"
COIN_EVALUATION_METHOD = "tp_sl_first_touch_adaptive_bracket_24h_v5"
# Training/holdout observation window is NOT a live virtual-position expiry.
STRESS_ROUND_TRIP_COST = 0.0040
MIN_VALID_RESOLVED = 35
PURGE_MS = (EVALUATION_BARS + 1) * INTERVAL_MS



def _time_of(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


class CoinModelRegistry:
    def __init__(self, data_dir, max_in_memory=8):
        self.directory = Path(data_dir) / "coin_models_v60"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_in_memory = max(1, int(max_in_memory))
        self.metadata = {}
        self.cache = OrderedDict()
        for path in self.directory.glob("*.json"):
            try:
                v = json.loads(path.read_text(encoding="utf-8"))
                symbol = v.get("symbol")
                # Reject unexpected file name/metadata combinations. This also
                # Loads ONLY independently tested V6.0 records; earlier model files are left intact.
                if (symbol_valid(symbol) and path.stem == symbol_file_key(symbol)
                    and v.get("model_family") == MODEL_FAMILY
                    and self._artifact_path(symbol).exists()):
                    self.metadata[symbol] = v
            except (OSError, ValueError, TypeError):
                LOG.warning("Coin model metadata could not be read: %s", path.name)

    def _artifact_path(self, symbol):
        if not symbol_valid(symbol):
            raise ValueError("Invalid Futures symbol")
        return self.directory / f"{symbol_file_key(symbol)}.joblib"

    def _meta_path(self, symbol):
        if not symbol_valid(symbol):
            raise ValueError("Invalid Futures symbol")
        return self.directory / f"{symbol_file_key(symbol)}.json"

    def info(self, symbol):
        return self.metadata.get(symbol)

    def version(self, symbol):
        return (self.info(symbol) or {}).get("version")

    def ready(self, symbol):
        return symbol in self.metadata and self._artifact_path(symbol).exists()

    def permissions(self, symbol):
        return model_permissions(self.info(symbol))

    def training_due(self, symbol, now=None, interval_hours=6, retry_seconds=3600):
        now = now or time.time()
        info = self.info(symbol)
        if info is None:
            return True
        # Last successful per-symbol training, not the global model's time.
        trained = _time_of(info.get("trained_at"))
        return now - trained >= interval_hours * 3600

    def stats(self, eligible=None):
        pool = set(eligible) if eligible is not None else set(self.metadata)
        trained = [s for s in pool if self.ready(s)]
        approved_long = sum(self.permissions(s)["LONG"]["approved"] for s in trained)
        approved_short = sum(self.permissions(s)["SHORT"]["approved"] for s in trained)
        return {"eligible": len(pool), "trained": len(trained),
                "pending": max(0, len(pool)-len(trained)),
                "approved_long": approved_long, "approved_short": approved_short,
                "both_directions_blocked": sum(not (self.permissions(s)["LONG"]["approved"] or self.permissions(s)["SHORT"]["approved"]) for s in trained)}

    @staticmethod
    def decision(p_long, p_short):
        """Independent TP-first probabilities. Never treat these as a 3-class simplex."""
        p_long = float(p_long) if p_long is not None else 0.0
        p_short = float(p_short) if p_short is not None else 0.0
        # If two incompatible directions are simultaneously credible, wait.
        if p_long >= SIGNAL_THRESHOLD and p_long >= p_short + .10:
            return "LONG", p_long
        if p_short >= SIGNAL_THRESHOLD and p_short >= p_long + .10:
            return "SHORT", p_short
        return "BEKLE", 0.0

    @staticmethod
    def _candidates():
        # Existing scikit-learn only; bounded CPU/RAM for 1 vCPU, 2GB.
        return (
            ("LogisticRegression", make_pipeline(StandardScaler(),
                LogisticRegression(C=0.35, max_iter=240, random_state=42))),
            ("HistGradientBoosting", HistGradientBoostingClassifier(
                max_iter=55, max_leaf_nodes=7, min_samples_leaf=24,
                learning_rate=.06, l2_regularization=5.0, random_state=42)),
        )

    @staticmethod
    def _predict_tp(model, data):
        # Class 1 = TP touched before SL, class 0 = SL touched before TP.
        if model is None:
            return np.zeros(len(data), dtype=float)
        order = list(model.classes_)
        return np.asarray(model.predict_proba(data)[:,order.index(1)], dtype=float)

    @staticmethod
    def calibrated(raw, calibration):
        """Validation-only Brier-optimized shrinkage of overly confident TP scores.

        Never uses the chronological holdout for fitting calibration. Choosing
        alpha=0 (baseline only) must NOT qualify a direction for live signals.
        """
        raw = np.asarray(raw, dtype=float)
        if not isinstance(calibration, dict):
            return np.zeros_like(raw)
        prior = float(calibration["validation_prior"])
        alpha = float(calibration["alpha"])
        return np.clip(prior + alpha * (raw - prior), 0, 1)

    @staticmethod
    def fit_calibration(raw_validation, labels, train_base_rate):
        """Conservative monotone calibration, fitted on validation only.

        Restrict complexity to a one-dimensional shrink to avoid inventing
        certainty from ~2 days of highly autocorrelated 15m observations.
        """
        raw = np.clip(np.asarray(raw_validation,dtype=float),0,1)
        labels = np.asarray(labels,dtype=int)
        if len(labels)<MIN_VALID_RESOLVED:
            return None
        prior = float((labels.sum()+12*train_base_rate)/(len(labels)+12))
        candidates = (0., .25, .5, .75, 1.)
        scores = [float(np.mean((labels-(prior+a*(raw-prior)))**2)) for a in candidates]
        best = int(np.argmin(scores))
        return {"validation_prior":round(prior,8), "alpha":candidates[best],
                "validation_calibrated_brier":round(scores[best],7),
                "validation_resolved":int(len(labels)),
                "fit_source":"chronological_validation_only",
                "calibration_type":"Brier_selected_shrink_to_validation_base_rate"}

    def fit(self, symbol, candles):
        if not symbol_valid(symbol):
            raise ValueError("Invalid symbol")
        # Construct causal feature rows independently of any candidate bracket.
        # The V5.9 training=True helper dropped ambiguous fixed-2.5/1.5 labels;
        # reusing it would unfairly bias an optimized V6 bracket selection.
        frame = features(candles, training=False)
        if len(frame) <= EVALUATION_BARS:
            raise ValueError("Insufficient forward history for 24h TP/SL labels")
        frame = frame.iloc[:-EVALUATION_BARS].copy()
        if len(frame) < 550:
            raise ValueError("Insufficient real independent 15m candle history")
        times = np.sort(frame["time"].unique())
        if len(times) < 550:
            raise ValueError("Too few distinct completed candles")
        val_start = times[int(len(times)*.65)]
        test_start = times[int(len(times)*.80)]
        train = frame[frame["time"] < val_start - PURGE_MS]
        valid = frame[(frame["time"] >= val_start) & (frame["time"] < test_start - PURGE_MS)]
        test = frame[frame["time"] >= test_start]
        if len(train) < 240 or len(valid) < 55 or len(test) < 100:
            raise ValueError("Purged chronological split too short")
        # TP and SL multipliers are chosen strictly from PURGED TRAIN data,
        # frozen BEFORE either validation or holdout is assessed. All model
        # labels, holdout returns, and live/paper targets use the SAME bracket.
        risk_plan, bracket_labels = choose_on_train(candles, frame, int(val_start-PURGE_MS))
        train = train.copy(); valid = valid.copy(); test = test.copy()
        for direction, col, j in (("LONG","long_outcome",0),("SHORT","short_outcome",1)):
            bp = risk_plan[direction]
            outcome = bracket_labels[(bp['tp_atr'],bp['sl_atr'])][:,j]
            # Lookup by position in the unchanged chronologically sorted frame.
            frame[col] = outcome
            train[col] = frame.loc[train.index,col].to_numpy()
            valid[col] = frame.loc[valid.index,col].to_numpy()
            test[col] = frame.loc[test.index,col].to_numpy()
        X_test = test[COLS].to_numpy(dtype=float)
        models = {}
        model_comparison = {}
        test_probs = {}
        calibrations = {}
        for direction, col in (("LONG", "long_outcome"), ("SHORT", "short_outcome")):
            # An unresolved 24-hour window is CENSORED, not an SL and not
            # a manufactured TP. Contradictory same-bar highs/lows are excluded.
            tr = train[train[col].isin([-1,1])]
            va = valid[valid[col].isin([-1,1])]
            tr_y = (tr[col].to_numpy(dtype=int) == 1).astype(int)
            va_y = (va[col].to_numpy(dtype=int) == 1).astype(int)
            comparison = {"train_resolved":int(len(tr)), "train_censored":int(len(train)-len(tr)),
                          "validation_resolved":int(len(va)), "validation_censored":int(len(valid)-len(va)),
                          "candidates":{}, "chosen":None,
                          "probability_meaning":"TP first, conditional on a TP/SL touch observable within 24h; not an indefinite-horizon TP probability"}
            model_comparison[direction] = comparison
            if (len(tr) < 140 or len(va) < MIN_VALID_RESOLVED
                or len(set(tr_y)) != 2 or len(set(va_y)) != 2
                or min(np.bincount(tr_y)) < 15):
                comparison["reason"] = "Insufficient resolved TP/SL events or two classes"
                models[direction] = None
                calibrations[direction] = None
                test_probs[direction] = np.zeros(len(test))
                continue
            x_tr = tr[COLS].to_numpy(dtype=float)
            x_va = va[COLS].to_numpy(dtype=float)
            baseline_rate = float(tr_y.mean())
            baseline_brier = float(np.mean((va_y-baseline_rate)**2))
            comparison["validation_baseline_brier"] = round(baseline_brier,6)
            champion = None
            best_brier = float("inf")
            for name, candidate in self._candidates():
                try:
                    candidate.fit(x_tr, tr_y)
                    predicted = np.clip(self._predict_tp(candidate,x_va),.000001,.999999)
                    brier = float(brier_score_loss(va_y,predicted))
                    comparison["candidates"][name] = {"validation_brier":round(brier,6),
                            "validation_samples":len(va)}
                    # Prefer simpler LogisticRegression on practically tied metrics.
                    if brier < best_brier - .005 or champion is None:
                        champion = (name,candidate)
                        best_brier = brier
                except (ValueError, FloatingPointError) as exc:
                    comparison["candidates"][name] = {"error":str(exc)[:160]}
            # Avoid a complex winner without evidence it beats a non-ML base rate.
            # Still persist a candidate for audit, but never approve it for trading.
            if champion is None:
                models[direction] = None
                test_probs[direction] = np.zeros(len(test))
                comparison["reason"] = "Both candidate algorithms failed"
                continue
            name, winner = champion
            comparison["chosen"] = name
            comparison["chosen_validation_brier"] = round(best_brier,6)
            comparison["beat_baseline"] = best_brier < baseline_brier - .005
            comparison["validation_win_rate"] = round(float(va_y.mean()),5)
            models[direction] = winner
            calibration = self.fit_calibration(
                self._predict_tp(winner,x_va), va_y, baseline_rate)
            calibrations[direction] = calibration
            comparison["calibration"] = calibration
            test_probs[direction] = self.calibrated(
                self._predict_tp(winner, X_test), calibration)
        if all(model is None for model in models.values()):
            raise ValueError("No direction has sufficient resolved TP/SL events for training")
        p_long = test_probs["LONG"]
        p_short = test_probs["SHORT"]
        actions = np.zeros(len(test),dtype=int)
        for i in range(len(test)):
            d,_ = self.decision(p_long[i],p_short[i])
            actions[i] = 1 if d == "LONG" else -1 if d == "SHORT" else 0
        outcomes = test[["long_outcome","short_outcome"]].to_numpy(dtype=int)
        atrs = test["atr_pct"].to_numpy(dtype=float)
        test_times = test["time"].to_numpy(dtype=np.int64)
        validation = {
            "LONG": tp_sl_direction_metrics(actions, outcomes[:,0], atrs, test_times, 1,
               tp_atr=risk_plan['LONG']['tp_atr'], sl_atr=risk_plan['LONG']['sl_atr']),
            "SHORT": tp_sl_direction_metrics(actions, outcomes[:,1], atrs, test_times, -1,
               tp_atr=risk_plan['SHORT']['tp_atr'], sl_atr=risk_plan['SHORT']['sl_atr']),
        }
        for direction, sign in (("LONG",1),("SHORT",-1)):
            idx = np.flatnonzero(actions==sign)
            resolved = [i for i in idx if outcomes[i,0 if sign==1 else 1] in (1,-1)]
            br=risk_plan[direction]
            stressed = [(br['tp_atr'] if outcomes[i,0 if sign==1 else 1]==1 else -br['sl_atr'])*float(atrs[i])
                        - STRESS_ROUND_TRIP_COST for i in resolved]
            detail = model_comparison[direction]
            permitted = bool(detail.get("beat_baseline"))
            validation[direction]["validation_outperformed_baseline"] = permitted
            calibration = calibrations.get(direction)
            validation[direction]["calibration_alpha"] = (
                calibration.get("alpha") if calibration else None)
            validation[direction]["calibration_sample_count"] = (
                calibration.get("validation_resolved") if calibration else 0)
            validation[direction]["probability_calibration_method"] = (
                calibration.get("calibration_type") if calibration else None)
            validation[direction]["mean_stressed_net_return"] = (
                round(float(np.mean(stressed)),6) if stressed else None)
            validation[direction]["stress_cost_assumption"] = STRESS_ROUND_TRIP_COST
            validation[direction]["model_choice"] = detail.get("chosen")
            # Overlapping 24h first-touch labels are correlated: effective
            # independent evidence is smaller than raw 15m signal count.
            independent = 0
            last_independent = -1_000_000_000_000_000
            for i in idx:
                at = int(test_times[i])
                if at - last_independent >= EVALUATION_BARS * INTERVAL_MS:
                    independent += 1
                    last_independent = at
            validation[direction]["independent_signal_windows"] = independent
            valid_look = test[test[("long_outcome" if sign==1 else "short_outcome")].isin([-1,1])]
            if len(valid_look) and detail.get("chosen"):
                y = (valid_look[("long_outcome" if sign==1 else "short_outcome")].to_numpy() == 1).astype(int)
                pred = self.calibrated(
                    self._predict_tp(models[direction],valid_look[COLS].to_numpy(dtype=float)),
                    calibrations.get(direction))
                validation[direction]["holdout_brier"] = round(float(brier_score_loss(y,np.clip(pred,0,1))),6)
                validation[direction]["holdout_baseline_brier"] = round(float(np.mean((y-detail["validation_win_rate"])**2)),6)
                validation[direction]["holdout_outperformed_baseline"] = bool(
                    validation[direction]["holdout_brier"] < validation[direction]["holdout_baseline_brier"] - .002)
        # Independent deterministic multi-timeframe technical watchlist audit.
        # It must not borrow AI permissions, fabricate a probability, or pass a
        # technical direction unless a separately frozen holdout supports it.
        tech_l, tech_s = setup_arrays(test)
        technical_validation = {}
        for direction, vals, other, column in (
                ("LONG", tech_l, tech_s, "long_outcome"),
                ("SHORT", tech_s, tech_l, "short_outcome")):
            positions = np.flatnonzero((vals >= 6.5) & (vals >= other+1.5))
            resolved = [int(i) for i in positions if int(test.iloc[i][column]) in (1,-1)]
            hits = sum(int(test.iloc[i][column])==1 for i in resolved)
            br=risk_plan[direction]
            stress = [((br['tp_atr'] if int(test.iloc[i][column])==1 else -br['sl_atr'])*float(atrs[i])
                       - STRESS_ROUND_TRIP_COST) for i in resolved]
            independently_spaced=0
            last=-10**20
            for i in resolved:
                stamp=int(test_times[i])
                if stamp-last >= EVALUATION_BARS*INTERVAL_MS:
                    last=stamp;independently_spaced+=1
            from quality_guard import wilson_lower
            n=len(positions);r=len(resolved)
            hit_rate=hits/r if r else None
            mean=float(np.mean(stress)) if stress else None
            lower=wilson_lower(hits,r) if r else None
            # These are conservative exploratory history gates, not a promise
            # of future profitability; multiple-testing risk remains material.
            allowed=(n>=25 and r>=25 and r/n>=.85
                     and independently_spaced>=4 and hit_rate>=.58
                     and lower>=.43 and mean is not None and mean>=.0006)
            technical_validation[direction]={
                "approved":bool(allowed),"signals":n,"resolved":r,"winning":hits,
                "hit_rate":round(hit_rate,5) if hit_rate is not None else None,
                "wilson_lower_95":round(lower,5) if lower is not None else None,
                "independent_24h_windows":independently_spaced,
                "mean_stressed_return":round(mean,6) if mean is not None else None,
                "frozen_tp_atr":br['tp_atr'],"frozen_sl_atr":br['sl_atr'],
                "stress_total_cost":STRESS_ROUND_TRIP_COST,
                "test_method":"fixed_score>=6.5; purged chronological unseen TP/SL first touch (24h censored)",
                "notice":"Exploratory technical confirmation; historical correlation and multiple-comparison risk. Not guaranteed live P(TP)."}
        date = datetime.now(timezone.utc)
        version = date.strftime("coin-%Y%m%dT%H%M%SZ") + "-" + symbol + "-v60"
        info = {
            "symbol": symbol, "version":version, "model_family":MODEL_FAMILY,
            "algorithm":"Per-direction logistic/gradient booster + training-frozen TP/SL grid and validation Brier shrinkage",
            "trained_at":date.isoformat(), "train_samples":len(train),
            "validation_samples":len(valid), "test_samples":len(test),
            "last_labelled_candle":int(frame["time"].max()),
            "evaluation_method":COIN_EVALUATION_METHOD,
            "directional_validation":validation,
            "technical_validation":technical_validation,
            "risk_plan":risk_plan,
            "strategy_selection":"Training-only 4-grid ATR brackets; purged validation and unseen holdout do not tune TP or SL", 
            "technical_indicators":len(COLS),
            "technical_summary":"15m MACD/BB/ADX/RSI/Stoch/MFI/CMF/VWAP/volume, candle candidates, support/resistance, completed 1h+4h trend",
            "model_comparison":model_comparison,
            "trade_exit_method":"TP_SL_FIRST_TOUCH_NO_TIME_LIMIT",
            "label_evaluation_lookahead_bars":EVALUATION_BARS,
            "probability_definition":"Conservatively shrunk ML score for P(TP first | either TP/SL observed within 24h), NOT a verified live success probability",
            "historical_candle_target":4500,
            "risk_note":"Finite 24h training labels are right-censored; live paper positions NEVER expire on a timer. Each direction has independent conditional probability, so p_long+p_short need not equal 1. 0.16% standard and 0.4% stressed total costs are assumptions; funding/liquidation/liquidity/real fills excluded.",
        }
        artifact = self._artifact_path(symbol)
        meta_path = self._meta_path(symbol)
        file_key = symbol_file_key(symbol)
        tmp_model = self.directory / f".{file_key}.tmp.joblib"
        tmp_info = self.directory / f".{file_key}.tmp.json"
        joblib.dump({"models":models,"calibration":calibrations},tmp_model,compress=2)
        tmp_info.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding="utf-8")
        os.replace(tmp_model,artifact)
        os.replace(tmp_info,meta_path)
        self.metadata[symbol] = info
        self.cache[symbol] = {"models":models,"calibration":calibrations}
        self.cache.move_to_end(symbol)
        while len(self.cache)>self.max_in_memory:
            self.cache.popitem(last=False)
        return info

    def _load(self, symbol):
        if symbol in self.cache:
            self.cache.move_to_end(symbol)
            return self.cache[symbol]
        if not self.ready(symbol):
            return None
        try:
            # Only locally generated and persisted joblib files are loaded;
            # never untrusted user model uploads.
            model = joblib.load(self._artifact_path(symbol))
            self.cache[symbol] = model
            while len(self.cache) > self.max_in_memory:
                self.cache.popitem(last=False)
            return model
        except Exception as exc:
            LOG.warning("Cannot load per-symbol model %s: %s", symbol, exc)
            return None

    def infer(self, symbol, candles, now_ms=None):
        info = self.info(symbol)
        if info is None or info.get("model_family") != MODEL_FAMILY:
            return None
        models = self._load(symbol)
        if models is None or not isinstance(models,dict) or not isinstance(models.get("models"),dict):
            return None
        now_ms = now_ms or int(time.time()*1000)
        closed = sorted((c for c in candles if c["close_time"] < now_ms),key=lambda c:c["time"])
        if len(closed)<80 or now_ms-closed[-1]["close_time"]>35*60*1000:
            return None
        if any(closed[i]["time"]-closed[i-1]["time"]!=INTERVAL_MS
               for i in range(len(closed)-79,len(closed))):
            return None
        frame = features(closed[-500:])
        if frame.empty:
            return None
        last = frame.iloc[-1]
        technical = technical_scan(frame)
        technical_direction = technical["direction"]
        risk_plan = info.get('risk_plan') or {}
        def _multipliers(side):
            plan = risk_plan.get(side) or {}
            return float(plan.get('tp_atr', FALLBACK[0])), float(plan.get('sl_atr',FALLBACK[1]))
        technical_gate = (info.get("technical_validation") or {}).get(technical_direction or "", {})
        technical["status"] = ("DOĞRULANDI" if technical_gate.get("approved") else "TEST ADAYI") if technical_direction else "FIRSAT BEKLENIYOR"
        technical["holdout"] = technical_gate if technical_direction else None
        X = last[COLS].to_numpy(dtype=float).reshape(1,-1)
        p_long = float(self.calibrated(
            self._predict_tp(models["models"].get("LONG"),X),
            models["calibration"].get("LONG"))[0])
        p_short = float(self.calibrated(
            self._predict_tp(models["models"].get("SHORT"),X),
            models["calibration"].get("SHORT"))[0])
        candidate, confidence = self.decision(p_long,p_short)
        reason = None
        signal = candidate
        if signal != "BEKLE" and not self.permissions(symbol)[signal]["approved"]:
            reason = "TP/SL test veya stress maliyet onayi yok; guvenlik kilidi"
            signal,confidence="BEKLE",0.0
        entry = float(last["close"])
        atr = float(last["atr_pct"]*entry)
        if technical_direction in ("LONG", "SHORT"):
            sign = 1 if technical_direction == "LONG" else -1
            technical["entry_reference"] = entry
            tp_mult,sl_mult=_multipliers(technical_direction)
            technical["stop_estimate"] = entry - sign*sl_mult*atr
            technical["target_estimate"] = entry + sign*tp_mult*atr
            technical["risk_plan"]={"tp_atr":tp_mult,"sl_atr":sl_mult}
            technical["entry_timing"] = entry_guidance(last,technical_direction)
            technical["bracket_note"] = "Eğitimde seçilen ATR tabanlı örnek TP/SL; geçmiş holdout sonucu ayrıca denetlenir. Takip adayı işlem sinyali değildir"
        active_side = signal if signal in ('LONG','SHORT') else candidate
        tp_mult,sl_mult=_multipliers(active_side)
        # A positive-distance bracket is mandatory, even if ML is confident.
        if signal in ('LONG','SHORT') and atr*max(tp_mult,sl_mult)>=entry:
            reason='TP/SL seviyesi sifirin altinda; sinyal guvenlik kilidi'
            signal,confidence='BEKLE',0.0
        return {"signal":signal,"confidence":round(confidence,4) if signal!="BEKLE" else None,
                "p_long":round(p_long,4),"p_short":round(p_short,4),
                # LONG/SHORT scores are NOT a 3-class distribution.
                "p_wait":None, "blocked_reason":reason,
                "candidate_signal":candidate if candidate != "BEKLE" else None,
                "entry_reference":entry,
                "stop_estimate":(entry-sl_mult*atr if signal=="LONG" else entry+sl_mult*atr if signal=="SHORT" else None),
                "target_estimate":(entry+tp_mult*atr if signal=="LONG" else entry-tp_mult*atr if signal=="SHORT" else None),
                "tp_atr":tp_mult,"sl_atr":sl_mult,
                "strategy_validation":(info.get('directional_validation') or {}).get(active_side),
                "entry_timing":entry_guidance(last,active_side),
                "risk_atr_pct":float(last["atr_pct"]),"candle_time":int(last["time"]),
                "model_version":info["version"],"technical":technical,"timeframe":"15m", "horizon_minutes":None,
                "probability_definition":info["probability_definition"],
                "risk_exit_method":"tp_sl_first_touch_no_expiry", "model_scope":"coin_specific"}
