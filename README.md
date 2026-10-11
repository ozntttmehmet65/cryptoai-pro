# CryptoAI Pro V7.0

Read-only Binance futures analysis and forward paper observations. No exchange orders are sent.

The former Dockerfile embedded an XZ application archive. The identical application was extracted into `source/`; Docker now builds directly from readable source. Existing `/app/data`, `signals.sqlite`, and `coin_models_v60` paths and model evaluation families are preserved.

V6.2 adds one shared entry veto for the market list, detail page, opportunities, health counts, and REST/WebSocket paper entry. Quotes must be under 10 seconds old, entries within 15 minutes of candle close, reference movement no more than 0.35 ATR, and theoretical TP distance greater than the existing 0.16% assumed round-trip cost. Current model permission and live direction drift checks remain mandatory. These are operational filters, not a newly backtested strategy or a claimed increase in win rate. Model scores and confirmation points are not guaranteed TP probabilities.

The paper page reports separate LONG/SHORT results, Wilson lower bounds, sample size, data gaps and theoretical profit factor. Shadow, legacy, unresolved and unverified observations are never counted as successful closed approved signals. Open and unverified totals remain visible. Wilson intervals assume independent samples; correlated coins can violate that assumption. Paper observations omit real executions, variable spread, funding and liquidation.

## Verify locally

```sh
python -m pip install -r source/requirements.txt
python -m unittest discover -s tests -v
node --check source/static/app.js
```

## Run

```sh
cd source
DATA_DIR=/path/to/persistent/data uvicorn app:app --host 0.0.0.0 --port 10000 --workers 1
```

The existing Render Docker service can deploy this repository without a new service or disk. Keep its existing persistent disk mount and environment variables. No production profit benchmark has been performed for the added entry veto; validate via forward observation before interpreting results as predictive improvement.

## V7.0 execution policy and frozen diagnostics

Adds a single public USD-M all-book-ticker WebSocket. Exchange event timestamps and book update IDs reject replayed/out-of-order records. All views and forward paper entry require a fresh best bid/ask (12s), spread <=8bps and <=0.10 ATR, and at least 1,000 USDT visible on EACH best level. These are predeclared conservative policy limits, not empirically optimized profit rules. Full depth, actual fills, order size, funding and liquidation are not measured.

A conditional TP-score check compares the calibrated score with the 0.4% stressed round-trip assumption plus observed spread and a five-percentage-point margin. The underlying score is conditional on resolution inside 24 hours, not an unconditional real-world expected return. The shared gate can only veto; it cannot override model validation. Global direction and symbol/model-specific forward drift locks both apply. Quotes can recover an eligible entry inside its original window, without duplicating an approved observation. Shadow tracks have separate identities and cannot block an approved track. Entry-policy cohorts distinguish V7.0 observations from older results.

New sequential model training additionally saves non-overlapping 24h candidate diagnostics, three frozen chronological test segments, reliability bins and calibration ECE. Unresolved independent candidates remain in the denominator; segments and bins are diagnostic and NEVER select parameters. Existing models remain usable under their original validation. New artifacts contain a version/family identifier checked against metadata; inference rejects an older feature row masquerading as the latest candle.

18 regression tests plus a synthetic training/reload/inference integration check and eight local HTTP endpoints were verified. Synthetic fixtures are test data only and never production market evidence. No forward improvement claim is made; V7.0 needs new observations.
