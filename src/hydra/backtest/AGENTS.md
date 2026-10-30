# src/hydra/backtest

Backtests, hyperopt, walk-forward and the durable job queue that the `hydra-backtest-worker` runs.
Repo-wide rules are in the root `AGENTS.md`.

## Commands

```bash
uv run pytest tests/unit/test_backtest -q -m "not slow"
uv run mypy src/hydra/backtest/ --strict      # CI checks this package strict
uv run pytest tests/integration/test_backtest_jobs.py tests/integration/test_backtest_real_data.py -m integration  # HYDRA_DB_*
```

## Invariants

Test files without a `tests/` prefix are in `tests/unit/test_backtest/`.

- Bars come only from `ts.ohlcv_1m` (every timeframe, keyed by a `timeframe` column) through `data.py`, which
  records `DataProvenance` and raises `NoMarketDataError` (`no_market_data`) when nothing is stored
  (`tests/integration/test_backtest_real_data.py::test_backtest_runs_on_stored_bars_with_exact_provenance`).
- Missing ranges are fetched before the run by ONE `OnDemandFetchService` per process: `worker.py` `fetch_engine()`
  and `services/strategies.py` `preview_fetch_engine()`. With `data.auto_fetch_enabled=false` both are `None`: no
  fetch phase, and submit answers 422 with the stored range (`test_worker.py::test_the_kill_switch_skips_the_fetch_phase`,
  `tests/integration/test_backtest_real_data.py::test_no_stored_bars_is_422_with_available_range`).
- `runner.py` publishes each bar on a paper `SessionRuntime` bus, so executor, sizing, pre-trade checks and
  protective exits are the paper session's code; an in-memory `RiskService` runs on bar time with the drawdown
  tiers off; metric writes are muted. Never add a backtest-only fill path
  (`test_execution_parity.py`, `test_perp_backtest.py::test_perp_paper_equals_perp_backtest_on_the_same_bars`).
- A run scores only the bars its strategy can trade: the warm-up (`runner.strategy_warmup`, the instance's
  `required_history`, override headroom included) is replayed with `warmup_bars` but excluded from equity, metrics and
  the benchmark. Plain backtests, hyperopt trials and walk-forward in-sample trials pass it (in-sample trials warm from
  the bars before the window when a rolling fold has them: `optimise(lead_bars=...)`); the holdout and OOS runs warm
  from the preceding window. A window the warm-up consumes whole is a `no_trades` trial. Never run with `warmup_bars=0`
  on a strategy that gates on `required_history` (`test_warmup_convention.py`).
- Metric definitions and undefined cases (None or 0, never inf) live only in the `metrics.py` docstring; change
  it with the code (`test_metrics.py`).
- Hyperopt keeps a fixed final 20 % holdout (`HOLDOUT_FRACTION`) that no trial sees; crashing, idle and
  non-finite-Sharpe trials never win (`test_hyperopt.py`). DSR reproduces the paper's 0.9004
  (`test_validation.py`).
- Walk-forward `fold_count` is exactly the number of out-of-sample folds: `walkforward.fold_windows` splits the bars
  into one in-sample window plus `fold_count` out-of-sample windows (in-sample `train_pct / (1 - train_pct)` times
  as long) that tile the range to its last bar, and returns None when a window is under `MIN_WINDOW_DAYS` (30), which
  the submit answers 422 `range_too_short` (`tests/unit/test_backtest/test_walkforward.py::test_the_windows_tile_the_span_with_exactly_fold_count_folds`,
  `::test_the_analyzer_runs_exactly_fold_count_folds`).
- Hyperopt and walk-forward never vary the ML-native `model_name` (a run serves only the model attached before it):
  submit answers 422 `param_not_tunable`, and the optimiser refuses before any trial
  (`test_hyperopt.py::test_the_worker_never_runs_a_trial_over_the_ml_native_model_name`). A research job may pin
  `model_version_id`; see `src/hydra/ml/AGENTS.md`.
- Queue (`jobs.py`, `backtest_jobs`, kinds `backtest`/`hyperopt`/`walkforward`/`ml_train`): `claim` takes the oldest
  queued row `FOR UPDATE SKIP LOCKED`; cancel is cooperative; `JOB_TIMEOUT_S` is 6 h; SIGTERM requeues without
  counting the attempt (`tests/integration/test_backtest_jobs.py`).
- Any number of workers share the queue. A running job is leased to its `worker_id`: the 10 s tick renews
  `heartbeat_at`; idle workers sweep every 30 s and requeue rows whose heartbeat is older than
  `backtest.job_lease_s` (180 s), failing them after 3 attempts; at startup a worker requeues only its own previous
  incarnation's rows. `tick`, `finish` and `requeue_for_shutdown` are fenced by `worker_id`, so a worker whose lease
  expired gets `LeaseLostError` and abandons the job without writing
  (`tests/integration/test_backtest_jobs.py::test_two_workers_a_live_peer_keeps_its_job_and_a_stale_one_is_requeued`,
  `::test_a_worker_whose_lease_expired_abandons_the_running_job`). `WORKER_ID` must be unique per live worker.
- The worker never migrates; it waits for the Alembic head (`tests/integration/test_backtest_jobs.py::test_worker_waits_for_schema`).
  `python -m hydra.backtest.worker check` (the healthcheck) passes while the heartbeat file is at most 90 s old.
- USD-M perp strategies run in every job kind: the worker's funding preflight (`worker._funding_preflight`) loads the
  job window's rows once and hyperopt and walk-forward pass them to every runner call; the executor accrues only rows
  inside each run's own bars (`tests/unit/test_backtest/test_perp_research.py`,
  `tests/unit/test_services/test_research.py::test_hyperopt_and_walkforward_accept_a_futures_strategy`).
- Every declared timeframe is its own stream: `jobs.ensure_job_bars` fetches each, `load_job_extra_bars`
  loads the non-primary ones (a missing one refuses by name), the runner replays them merged with the
  primary stream in close-time order, and the submit preflight 422s naming a declared timeframe with no
  stored data — an empty confirmation stream would silently never veto.
- Every fill is capped to `max_participation` (10 % by default) of the bar's volume: entry market
  orders fill the fillable slice and cancel the remainder; resting orders slice per bar and the
  remainder keeps working, same order id. One logical exit's slices aggregate into one round trip
  (`tests/unit/test_backtest/test_volume_fills.py`).

## Where things are

- `runner.py` (the replay), `data.py` (bar loading), `jobs.py` (requests, sizing, queue), `worker.py` (the process).
- `hyperopt.py`, `walkforward.py`, `validation.py` (PSR/DSR/CSCV PBO), `metrics.py`.
- `perp_funding.py`: tops up `ts.funding_rates` before a perp backtest.
