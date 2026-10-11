# CryptoAI Pro V7.3

Read-only Binance USD-M futures analysis and forward paper observations. No exchange order is sent. Docker builds the readable `source/` tree; the existing `/app/data` disk, SQLite database and `coin_models_v60` paths are preserved.

## Professional main-system policy

Each coin and direction trains LogisticRegression, HistGradientBoosting and bounded 48-tree ExtraTrees members. At least two members are mandatory; there is no single-model fallback. Inverse validation-Brier weights and aggregate shrink calibration use validation only. A frozen consensus requires two calibrated member scores >=0.60, their weight >=0.60 and maximum score spread <=0.20. Legacy directional score >=0.65 remains mandatory. V7.3 models use a candidate threshold chosen on chronological validation and frozen before holdout; the opposite-direction gap >=0.10 remains mandatory.

V7.2 adds nine controls:

1. **Longer real history and fixed time periods.** Target 18,000 genuine consecutive 15-minute candles, minimum 9,000 for direction approval. Older history uses bounded, paced REST pages, checkpointed after each success; migration continues sequentially. New listings receive no fabricated bars. Existing chronological train/validation/test splits purge the 24-hour label overlap. One frozen candidate is tested over three chronological thirds of its untouched holdout; these are not separately fitted rolling models.
2. **Bid/ask forward paper and costs.** New LONG entries use ask, SHORT entries bid; exits use the opposite side. Observed spread is embedded in prices. Explicit assumptions: 0.06% fee and 0.02% adverse slippage per leg on a hypothetical 100-unit quote notional. TP exits cap favourable overshoot. Missing exit size or a book gap over three minutes becomes unverified. Positions have no timer expiry. Published historical funding events and their mark prices are accounted for across entry < event <= exit. Net return remains absent until the entire interval has certified funding coverage. Neither fills, account fees, leverage nor liquidation are measured.
3. **Persistent quality reports.** Istanbul calendar-day decision counts, veto reasons, regimes, TP/SL, funding-pending exits, mean closed net return and negative-result sequences. Coin/candle/version identities prevent duplicate decisions. Snapshots refresh every five minutes, including the previous six completed days to incorporate delayed funding; decisions retain 14 days and daily snapshots 90 days. Earlier snapshots remain dated measurements.
4. **Causal regime filter.** Fixed rules from already closed ATR, volatility, ADX and 4-hour trend classify uptrend/downtrend/range/extreme. Extreme regimes veto entries. Other regimes need at least four independent holdout resolutions and positive stressed mean; they still need all direction and execution gates.
5. **Exposure correlation groups.** At most 60 current approved/follow candidates are compared on matched recent closed-candle returns, with >=64 common returns and exposure correlation >=0.85. SHORT exposure reverses return signs. Missing timestamps are never filled. Connected groups expose related opportunities; they do not send orders or approve signals.
6. **Controlled model promotion and rollback.** Each candidate and prior active artifact are archived. A current approved professional incumbent remains active if a challenger fails any currently approved direction. Metrics from different test periods are not ranked as if directly comparable. Four recent versions plus the incumbent are retained. Token-protected rollback accepts only a matching professional artifact with current (<24-hour), approved evidence; its original date stays intact and normal entry/drift gates still apply.
7. **Multiple-selection screen.** At least 12 resolved candidates selected >=24 hours apart before outcomes are inspected, >=85% resolution, positive stressed mean and three positive time periods are required. Exact one-sided binomial tail is adjusted for a fixed 1,400 directional tests per cycle, alongside a family-adjusted Wilson lower bound against worst sampled stressed break-even. Binomial/Wilson calculations assume independence; spacing does not prove independence. This is not an error guarantee across repeated retraining cycles or proof of market profitability.
8. **Causality regression check.** Future price/volume mutations must leave earlier 58 features and regime classifications unchanged. Training rejects gapped, unsorted or invalid OHLC histories; inference rejects stale feature rows and incompatible artifact/policy versions.
9. **Forward calibration diagnostics.** Only new approved observations with observable TP/SL within 24 hours enter reliability bins, Brier and ECE. Late exits, mature unresolved windows, unknown data and shadow observations are reported separately. These are descriptive measurements and never automatically retune thresholds. Scores remain conditional on a TP/SL resolution within 24 hours; LONG and SHORT scores need not sum to one.

Older V7.1 voting artifacts are visible for audit but cannot approve a V7.2 direction until professional migration finishes. Training a model does not mean its direction passed. Failed or unapproved candidates never bypass the gates. Old last-trade paper results are stored and reported separately; they cannot establish V7.2 bid/ask performance.

## Shared execution veto

All market, detail, opportunity and paper-entry paths require current direction permission, fresh last quote (<10s), fresh bid/ask (<12s), entry within 15 minutes of candle close, displacement <=0.35 ATR, spread <=8bps and <=0.10 ATR, and >=1,000 quote units displayed on each best level. Stressed cost is 0.4% plus observed spread, with a five-point conditional-score margin. Global and symbol/model-specific forward drift vetoes use new approved observations. Consensus, regime, evidence and execution checks only veto; none can promote an otherwise unapproved model.

## Run and verify

```sh
python -m pip install -r source/requirements.txt
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 python -m unittest discover -s tests -v
node --check source/static/app.js
cd source
DATA_DIR=/path/to/persistent/data uvicorn app:app --host 0.0.0.0 --port 10000 --workers 1
```

Docker defaults `PRO_TRAIN_CANDLES=18000` (clamped to 9,000–18,000); older `TRAIN_CANDLES` does not reduce the professional minimum. Keep the existing Render service and persistent disk. No new service, paid model API, exchange credentials or resource plan is required.

Public audit endpoints include `/api/professional-report`, `/api/coin-models`, `/api/coin-model/{symbol}`, `/api/paper` and `/api/execution-monitor`. Rollback is `POST /api/admin/rollback/{symbol}?version=...` with the existing `X-Admin-Token`; absent/incorrect tokens reject access.

Verification includes synthetic 9,000-candle three-member training/reload, candidate rejection without incumbent replacement, artifact restoration, future-mutation checks, bid/ask/funding calculations, incomplete funding, missing book size, replay/gap handling, shadow separation, calendar midnight and late funding reports, interrupted history checkpoints and shared API gates. Synthetic fixtures are isolated test data, never production market evidence. Forward results must accumulate after deployment; no profit improvement has been established.


## V7.3: explain zero approvals and repair candidate generation

Production inspection on 2026-10-11 found 562 trained models, zero permitted
LONG/SHORT directions and a still-running V7.2 migration. BTC and SOL had no
holdout candidates; ETH LONG had five, below the existing 40-result floor.
Several directions also failed validation or holdout baseline comparisons.
Training success alone is not evidence that a direction should be approved.

- Fix RSI when downside is zero: uninterrupted gains produce RSI 100 rather
  than NaN (15m) or neutral RSI (1h/4h). Flat prices remain neutral.
- Use 1200 consecutive, completed, fresh 15m bars for inference rather than
  bootstrap histories of 110/120 bars. Reuse local training caches at startup;
  copy the recent training segment into live storage after retrieval. Binance
  pacing, cooldown and closed-bar validation remain unchanged. Recursive
  indicators still have small initialization differences versus full training.
- Choose one candidate threshold from the predeclared menu .55/.60/.65/.70/.75
  using chronological validation only, including the existing consensus veto.
  Freeze the result BEFORE computing holdout actions. Failed selection retains
  .65 for observation and blocks new-model approval. The .10 direction margin,
  member vote support, professional family screen, regime, stress and live
  entry checks are retained. Validation selection can overfit; holdout gates do
  not eliminate all statistical dependence or multiple-retraining risk.
- Pair the frozen policy in schema-3 metadata and model payload, including
  reload/restore checks. Legacy models retain their original candidate policy
  and migrate sequentially. Preserve approved incumbents and version archives.
- Add /api/signal-diagnostics, exact per-direction failed checks, actual versus
  required values, and the model-screen explanation. Counts of failed checks
  overlap; they are not separate models. /api/health identifies V7.3 and exposes
  the same diagnostic view. Model list rows include the frozen candidate cut.

Validation: 58 regression and integration tests passed, including the 47
existing V7.2 tests. JavaScript syntax and whitespace checks passed. Ten local
API endpoints were checked with a synthetic fit/reload/inference fixture.
Synthetic tests establish implementation behavior, not market profitability.
No TP improvement or nonzero approved signal count is promised. Production
models retrain sequentially and may continue to abstain if their evidence fails.
