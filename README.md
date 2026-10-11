# CryptoAI Pro V6.2

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
