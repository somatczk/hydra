# Hydra

Bitcoin trading platform under active rebuild. What runs today: a FastAPI engine and a Next.js dashboard that run
paper-trading sessions with rule-based strategies on Binance market data, and backtests on bars already stored in
TimescaleDB. Most risk, ML and multi-exchange features are not wired yet; the status table below says which.
**Live trading is not supported until P3.**

## Status

The table describes `main` as it is. Statuses:

- **Working**: proven by the named test or command.
- **Partial**: some of it works; the caveat says what does not.
- **Planned Px**: scheduled for that phase (see [Roadmap](#roadmap)).
- **Known gap: unscheduled**: not true today and not yet assigned to a phase.

Each PR that changes one of these rows updates the row.

| Area | Capability | Status |
|------|------------|--------|
| Exchanges | Binance, Bybit, Kraken, OKX, spot + futures | **Planned P1/P4.** `ExchangeClient` builds any ccxt class by name. Orders always use the futures class while prices come from the spot feed, and symbols carry no market type. P1 adds a Binance-only gate at session start (other exchanges rejected); other exchanges and perps come in P4. |
| Strategies | Visual rule builder + Python strategies | **Partial.** Rules are saved as YAML and evaluated. Stop-loss and take-profit from the builder are dropped (P1 stops). The builder preview runs on the latest stored bars (`test_preview_uses_the_latest_stored_bars`). |
| Strategies | Hot-reload | **Planned P2.** `StrategyEngine.reload_strategy` exists, but nothing in the app calls it; a session reads its YAML once. |
| Strategies | Built-in strategy classes and shipped configs | **Partial.** Modules in `strategy/builtin/`: breakout, composite, dca, grid, mean_reversion, ml_ensemble, momentum, rule_based, trend_following. DCA and Grid do not work in a session: a DCA deal stalls after its base order and a grid places a one-shot ladder (their order tracking needs the P1 OMS wiring). Shipped strategy configs all use the rule-based class; only templates use DCA and Grid. No test-generated configs are committed (`test_no_committed_test_strategy_yaml`). |
| Backtest | Runs only on stored bars, records provenance, 422 when there is no data | **Working.** `tests/integration/test_backtest_real_data.py::test_backtest_runs_on_stored_bars_with_exact_provenance`, `::test_no_stored_bars_is_422_with_available_range`, `tests/unit/test_dashboard/test_backtest_routes.py`. |
| Backtest | Results and trades persisted, identical after a restart | **Working.** `tests/integration/test_backtest_real_data.py::test_results_survive_a_restart`. Needs TimescaleDB (see [Testing](#testing)). |
| Backtest | Walk-forward / CPCV, PBO, DSR | **Planned P2.** Walk-forward does not re-optimise per fold, CPCV is not reachable from the API, PBO is not CSCV, and DSR is the mean Sharpe. |
| Backtest | VectorBT parameter sweeps | **Planned P2.** `vectorbt` is a dependency but is never imported; the sweep is a numpy SMA cross that ignores fees. |
| Backtest | Realistic fill simulation | **Planned P2.** No partial fills, funding or liquidation; slippage is uncapped. |
| Backtest | Durable job queue | **Planned P2.** Backtests, hyperopt and walk-forward run as in-process tasks in the engine; task state is lost on restart. The `backtest-worker` image runs an idle placeholder loop. |
| ML | XGBoost training | **Planned P5.** Training code has no data loader or entrypoint. There is no LSTM code; the "LSTM Momentum" placeholder was removed in P0-B. |
| ML | ONNX inference, model upload / promote / rollback | **Planned P5.** Inference works in isolation, but no strategy calls it, nothing reads the model stage, and rollback leaves no model in Production. |
| ML | Drift detection | **Planned P5.** A PSI function exists and nothing runs it. The API and UI report drift as `unknown` and retrain returns 501 (P0-C). |
| ML | ML overlay per strategy | **Planned P5.** `MLOverlayConfig` is parsed and never read. |
| Risk | 4-tier circuit breakers | **Built, session wiring P1-E.** `RiskService` (`src/hydra/risk/service.py`) keeps account equity (paper capital + carried PnL + running sessions at closed-bar marks), drawdown tiers at 1/5, 1/3, 2/3 and 1x `max_drawdown_pct`, the daily-loss halt latched to 00:00 UTC and the 4h loss-streak pause in `risk_state` (scope `paper`), and writes every transition, reset and kill-switch change to `risk_events`. Tier 3 calls a flatten callback (again every minute while positions remain, including after a restart), tier 4 latches the kill switch. After a restart nothing is evaluated and openings are refused until the owner calls `recovery_complete()` and every held position has a mark; a `paper_capital` change, including one made while the engine was down, goes through the audited paper reset, which rebases the ledger and leaves breakers alone. `/api/risk/circuit-breakers`, `/status`, `/live-status` and `/events` serve this state; reset needs `confirm_tier` and a reason, is audited first and is rolled back if it cannot be saved; `POST /api/risk/paper-account/reset` (`confirm: RESET`) is the operator's paper reset. Sessions do not feed it marks or fills until P1-E, so until then `/status`, `/circuit-breakers` and `/var` return 503 `risk_service_unavailable`, `/live-status` reports `circuit_breakers_state: unavailable`, and `/events` reads the audit table directly (`tests/unit/test_risk/`, `tests/unit/test_dashboard/test_risk_truth.py`, `tests/integration/test_risk_state_pg.py`). |
| Risk | Pre-trade checks | **Working at the OrderManager seam; session path P1-E.** Orders are priced at the latest mark; qty <= 0 and openings with no mark are rejected. Reducing orders (`reduce_only`, clamped to the position, or a sell within the held long) skip every entry check, so exits, stops and flattens are never blocked. Openings check kill switch, breakers, position size vs sleeve (halved at tier 1), gross exposure across sessions, concurrent positions, USDT-only balance, Binance lot / min-notional, rate limit. One lock is held from the position read (clamp and open/reduce classification) through `create_order`, so two exits racing for one position fill once (`tests/unit/test_execution/test_order_manager_risk.py`). The session path still builds a per-session snapshot until P1-E switches it to the `RiskService`. |
| Risk | Per-strategy risk overrides | **Not supported in P1.** `PUT /api/risk/config/{strategy_id}` returns 501; `GET` returns the global limits that are enforced. |
| Risk | Exchange-side stop-losses | **Planned P3.** `ExchangeSafetyManager` has no caller, and paper stops never trigger. |
| Risk | Quick-trade TP / SL / trailing stop | **Planned P1 (stops).** An entry that is not filled at once (any LIMIT quick-trade) returns 200 with `exit_orders_deferred: true`, and its TP / SL / trailing orders are never placed: nothing reads the flag, and the UI shows a success toast. On a filled entry, a failed attach logs a warning and still returns 200. Either way the position looks protected when it is not. |
| Risk | Kill switch | **Partial → P1.** It stops running sessions, and the UI shows active / inactive / unknown. The flag is persisted only when the engine has a DB pool (not the case under `deploy/compose.yml`), yet `POST /api/trading/kill-switch` reports "all trading halted" either way, so a restart forgets it. The flatten step fails and positions stay open. `RiskService.activate_kill_switch` now latches in memory before persisting (a failed write reports `persisted: false` and stays latched) and `release_kill_switch` is audited; `stop_all` and the routes switch to it in P1-E. |
| Paper trading | Per-strategy capital, P&L | **Partial → P1.** Per-strategy paper capital slices work. The executor now keeps a spot ledger (no naked shorts; fees charged in USDT and carried into net P&L, entry fee pro-rata on partial exits) with equity = USDT + base x last close (`tests/unit/test_execution/test_paper_spot_ledger.py`). Sessions do not use it yet: unrealized P&L is marked only once P1-E calls `on_closed_bar` from the bar handler. Until P1-C / P1-E move session and risk views to `get_equity()`, `get_balance()` returns only the USDT total (free + locked), so their balances-plus-position-value sums equal equity. Live P&L is 0. |
| Paper trading | Stops, OCO and limits fill on closed bars; state survives a restart | **Partial → P1.** `PaperTradingExecutor.on_closed_bar` evaluates resting orders per symbol with the [paper fill rules](#paper-fill-rules) (`tests/unit/test_execution/test_paper_trading.py`, `test_fill_rules.py`), and every change is persisted to `paper_accounts` with trade rows idempotent on `(session_id, fill_seq)` and per-session balance snapshots; `PaperTradingExecutor.restore` rebuilds the account exactly (`tests/integration/test_paper_state_persistence.py`). `restore` also rebuilds the fill history from the session's trade rows. A commit whose acknowledgement was lost is detected and adopted; any other writer of the same session halts the executor (`PaperSessionHaltedError`). Not wired yet: sessions still only set marks, so stops never trigger, and recovery still builds a fresh executor until P1-E calls `on_closed_bar`, `restore` and `flatten`. Until then, in a recovered paper session that had persisted state, the first order is applied in memory, its write halts the executor, and every later order is refused; nothing more is written for that session. |
| Paper trading | Testnet + paper by default | **Partial → P1.** The trading mode defaults to `paper`, and session `ExchangeClient`s default to `testnet=True`. `HYDRA_TESTNET` stops startup (use `HYDRA_TRADING__TESTNET`), and `trading.testnet` is never read. The settings page's balance / orders / positions endpoints use stored keys against mainnet (`testnet=False`), read-only. |
| Data | Redis Streams event bus | **Planned P1** (event-bus isolation decides the transport). Sessions use an in-process `InMemoryEventBus`; `RedisEventBus` is used only in tests. Redis is only health-pinged by `/api/system/health`. |
| Data | Live market data (closed bars, warm-up, gap fill, persistence) | **Partial → P1-E wiring.** The session feed publishes each closed Binance spot candle once, in order, from one `watch_ohlcv_for_symbols` loop per exchange; a failing or slow subscriber neither drops the bar for others nor touches the WebSocket (`tests/unit/test_data/test_bars.py`, `tests/unit/test_data/test_ingestion.py`, `tests/unit/test_core/test_event_bus_memory.py`). Every declared timeframe reaches the strategy context once; `on_bar` runs only on the primary timeframe, and when timeframes close together the higher one is published first (`tests/unit/test_strategy/test_engine.py`). After a WebSocket error or 60 s of silence (forced reconnect) the missed closed bars are fetched over REST and delivered with `replay=True` except the newest per stream: catch-up bars are persisted and, once a session opts in with `publish_replay=True` (P1-E, together with replay filters on its OMS and price handlers; off by default), enter the context, but only the newest is evaluated, so a signal on an older missed bar is skipped. A range REST cannot fill (outage, ban, lag) does not hold back live bars: the stream reports `stale` (`gap_unfilled`) and the range is retried in the background, then delivered as late `replay=True` bars and inserted in order in the context; a range the exchange still has no bars for after 5 answers is logged and given up. `FeedStatusEvent` reports `stale` (silence, overdue bar, consumer lag, unfilled gap) per scope and `live` per scope; entries may resume only when `feed.is_live(exchange_id)`. Warm-up REST-fills interior holes in the stored window as well as its tail, and a DB outage no longer drops bars (persistence retries until the feed stops). Live and backtest produce identical signals and indicators on the same stored bars (`tests/e2e/test_live_backtest_parity.py`), and the feed's upserts land where the backtester reads (`tests/integration/test_market_data_storage.py`). Until P1-E wires it into sessions: no warm-up from the DB (a strategy waits `required_history` closed bars), live bars are not persisted, catch-up bars are not published, the context is unbounded and nothing acts on `FeedStatusEvent`. Higher timeframes are separate streams, not resampled (P2). Backfill and `download_history.py` no longer store the forming candle. |
| Data | PostgreSQL as source of truth | **Partial → P1.** Backtest results and trades are persisted (P0-B). Live fills are not stored, paper state is not reloaded, and system config lives in memory. |
| Data | Decimal for financial values | **Known gap: unscheduled.** Decimal in domain types and fills; floats at the exchange boundary, in routes, VaR and the flatten path. |
| Dashboard | Next.js app, routes listed under [Dashboard routes](#dashboard-routes) | **Working.** `cd dashboard && npm run build` (CI job "Dashboard Build") prints the route list. |
| Dashboard | Risk and portfolio data report unavailable / unknown instead of made-up values | **Working.** `tests/unit/test_dashboard/test_risk_truth.py`, `tests/unit/test_dashboard/test_portfolio_routes.py`, `cd dashboard && npm test`. |
| Dashboard | Every page shows an error instead of defaults | **Known gap: unscheduled.** `/templates` swallows a failed fetch and shows "No templates available yet". `/` and `/portfolio` fall back to `trading_mode: 'paper'` when `/api/system/config` fails and show paper numbers with no error. `/portfolio` drops a failed strategy-list fetch silently. `/api/models` serves `version: v1.0.0` and `stage: Staging` for an `.onnx` without sidecar metadata. |
| Dashboard | Real-time WebSocket updates | **Known gap: unscheduled.** The engine serves `/ws/market`, `/ws/trades`, `/ws/portfolio`, `/ws/signals` and `/ws/risk`; only per-session trade fills are ever broadcast. The session page re-fetches its detail on a fill (P0-C), which fixed the NaN P&L. |
| Dashboard | TradingView charts | **Known gap: unscheduled.** A third-party TradingView embed on `/trading` only; every other chart is Recharts. |
| Security | API authentication | **Working (engine) → romulus F-net.** Every HTTP route and WebSocket except `/health` and `/metrics` needs a principal: Authelia's `Remote-User`, trusted only from a TCP peer inside `api.trusted_proxy_cidrs` (uvicorn runs with `proxy_headers=False`, so `X-Forwarded-For` cannot fake the peer), or `Authorization: Bearer $HYDRA_API_TOKEN` for services. `api.auth_mode: dev` trusts everyone and logs CRITICAL (local compose only). With no CIDRs and no token the API refuses everything (401 `auth_not_configured`). `/docs` and `/openapi.json` are protected too (`tests/unit/test_dashboard/test_auth_coverage.py`, `tests/unit/test_dashboard/test_security.py`). Production needs the romulus hydra-edge network and auth env before the P1 image bump. |
| Security | CSRF and WebSocket Origin | **Working.** Browser principals need `X-Hydra-Request: 1` on every non-GET, an allowed `Origin` (`api.cors_origins`) if one is sent, and `Sec-Fetch-Site` same-origin or none; WebSocket upgrades need an allowed `Origin`. CORS allows only `api.cors_origins` (`tests/unit/test_dashboard/test_csrf.py`). |
| Security | Signal webhook | **Partial → P1 (E).** `POST /api/signals/webhook` is behind API auth, disabled (503 `webhook_disabled`) until a webhook secret is set, compares the secret in constant time, and targets only the named strategy's running session (`tests/unit/test_dashboard/test_signals_webhook.py`). Its orders still bypass the pre-trade risk check until P1-E routes them through the OrderManager. `/api/signals/telegram` was removed. |
| Monitoring | Prometheus alert rules | **Working (rule logic) → P1 producers.** `promtool test rules deploy/prometheus-rules.test.yml` (CI job "promtool check + test") has a firing and a non-firing case for every alert, and `tests/unit/test_deploy/test_prometheus_rules.py` checks that every rule reads a series defined in the metric contract (`src/hydra/core/metrics.py`). Exchange API latency and errors are recorded for every call and outcome (`tests/unit/test_execution/test_exchange_client_metrics.py`). Feed staleness compares the feed's timestamp gauges, set on every WebSocket message and closed bar and removed when a feed stops, against Prometheus `time()` (`tests/unit/test_data/test_ingestion.py`). Drawdown (`hydra_drawdown_ratio`) only moves on fills inside a running session until P1 risk wiring. The reconciliation alert was removed: there is no reconciliation until P3. |
| Monitoring | Grafana dashboards (drawdown, P&L, heat, exchange errors) | **Partial → P1 ops.** Every panel in `deploy/grafana-dashboard.json` and `config/grafana/hydra-overview.json` reads only exported series with their real labels (`tests/unit/test_deploy/test_grafana_dashboards.py`). Rolling Sharpe, Model Accuracy and Drift PSI were removed because nothing produces them (Sharpe needs a daily-return series; ML is P5). Win Rate, Fill Rate and the overview's trade-count and trade-PnL panels were removed until P1 paper execution: paper fills are no longer counted twice, but nothing observes `hydra_trade_pnl` until P1-E counts `PaperFill.net_pnl`. Portfolio Heat, drawdown and P&L stay empty or flat until P1 risk wiring sets them, and Position Exposure stays empty until P1 session wiring sets per-session positions. Left for P1 ops: `config/grafana/hydra-system.json` still queries `hydra_signal_count` with a `strategy` legend (always empty), `HydraHighMemoryUsage` is kept, the `$exchange_id` variable is sourced from `hydra_trades_total`, and "Strategy Latency" plots order fill latency. |
| Monitoring | Telegram alerts | **Partial.** Paging goes through romulus Alertmanager to Telegram. The in-app bot was deleted in P0-D. |
| Engineering | mypy strict | **Partial → P1 ops.** CI runs `mypy --strict` over eight packages, and `pyproject.toml` relaxes strict mode for most packages through overrides. |
| Engineering | import-linter module boundaries | **Working.** `make imports` (`uv run lint-imports`) prints `Contracts: 1 kept, 0 broken.`; it runs in `make check` and in the CI "Lint & Format" job. Layers: dashboard > portfolio, backtest > execution > risk, ml > strategy > data > indicators > core, ops. |
| Engineering | CI runs unit / integration / e2e | **Working.** Integration and e2e run against TimescaleDB 2.30.2 and Redis 8 service containers with `HYDRA_REQUIRE_INFRA=1`, which turns any skipped integration or e2e test, or a tier module skipped at collection, into a failure. Tests under `tests/integration` and `tests/e2e` take their tier marker from the directory, so `-m` cannot drop one; there is no exit-code-5 escape (`tests/unit/test_repo/test_ci_gating.py::test_infra_tiers_run_on_service_containers_with_skips_as_failures`, `::test_require_infra_turns_tier_skips_into_failures`, `::test_require_infra_fails_collection_time_and_unmarked_tier_skips`). |
| Engineering | Images publish only after CI passes | **Working.** The publish jobs in `ci.yml` need every other job and run only on pushes to `main` and manual dispatch on `main`; a push that changes no image input publishes nothing. PRs build every image and start it in the "Image Smoke" job. Published base images are pinned by digest, so publish rebuilds on the base the smoke job ran (`tests/unit/test_repo/test_ci_gating.py::test_publish_needs_every_other_job_and_runs_only_on_main_pushes`, `::test_publish_skips_pushes_that_change_no_image_input`, `tests/unit/test_repo/test_toolchain_pins.py::test_published_images_pin_one_base_digest_per_image`). |
| Engineering | Python and uv pinned in one place | **Working.** CI runs Python 3.14 from `.python-version` and uv 0.13.3 (setup-uv, the `ghcr.io/astral-sh/uv` image in each Dockerfile; `[tool.uv] required-version >=0.12.18` refuses older uvs but admits Dependabot's); every `uv sync` is `--locked` (`tests/unit/test_repo/test_toolchain_pins.py`). |
| Engineering | JSON logs carry level, logger, timestamp and tracebacks | **Working.** Stdlib, structlog and uvicorn records, uvicorn's startup lines included, render as one JSON line each, with the traceback text; the image smoke job requires every engine and backtest log line to be JSON (`tests/unit/test_core/test_logging.py`). |
| Engineering | Every module has tests | **Known gap: unscheduled.** No tests for `backtest/vectorbt_research.py` or `ops/notifications.py`. `backtest/hyperopt.py` has no unit tests of its own; it is covered through the route tests and `tests/integration/test_backtest_real_data.py::test_hyperopt_grid_runs_on_stored_bars_reproducibly`. |
| Engineering | Dockerfile references in Makefile, compose files and CI publish job | **Working.** `tests/unit/test_repo/test_build_references.py`. |
| Engineering | One config, encrypted credentials, one schema source, fail-fast startup | **Working.** One `HydraConfig` is loaded once in the lifespan: env > `{HYDRA_ENV}.yaml` > `base.yaml` > defaults; unknown keys at any depth, unresolved `${VAR}` and infra keys in YAML stop startup (`tests/unit/test_core/test_config.py`). `HYDRA_CREDENTIAL_KEY` is required whenever `DATABASE_URL` is set and is never generated; rows it cannot decrypt are reported, not fatal (`tests/unit/test_core/test_encryption.py`, `tests/unit/test_dashboard/test_lifespan.py`). Alembic (`hydra.data.migrations`) is the only schema source and runs first under an advisory lock; a failed migration stops startup and leaves no partial schema (`tests/integration/test_migrations.py`, `tests/integration/test_startup_romulus_env.py`). `PUT /api/system/config` is not persisted yet (P1-F). |
| Engineering | One version (pyproject) reported everywhere | **Partial.** The engine package, OpenAPI and `PlatformConfig` report the pyproject version (`tests/unit/test_repo/test_version_single_source.py`). The dashboard sidebar still shows `dashboard/package.json`'s version (1.2.0); fixing it needs a build arg in `docker/dashboard/Dockerfile` (Known gap: unscheduled). |

## Architecture (as is)

```mermaid
graph LR
    UI[Next.js dashboard]

    subgraph Engine["hydra-engine (one process)"]
        API[FastAPI routes] --> SM[SessionManager]
        API --> BT[In-process backtests<br/>hyperopt, walk-forward]
        SM --> Bus[InMemoryEventBus<br/>one per session]
        Bus --> SE[StrategyEngine]
        SE --> OM[OrderManager]
        OM -->|pre-trade check| PT[PreTradeRiskManager]
        OM --> PE[PaperTradingExecutor]
        OM --> EC[ExchangeClient<br/>ccxt async, testnet default]
    end

    UI -->|HTTP /api, WS /ws| API
    SM -->|ccxt.pro watch_ohlcv| Binance[(Exchange)]
    EC --> Binance
    API -->|asyncpg| DB[(TimescaleDB)]
    BT -->|stored bars| DB
    API -.->|health ping only| Redis[(Redis)]
    Worker[backtest-worker container<br/>idle placeholder loop]
```

- The engine is one process: FastAPI, the session manager and every session's event bus. Redis is not on the trading path.
- The schema comes only from Alembic revisions in `src/hydra/data/migrations/versions/`. The engine upgrades to head as
  its first database step at startup, under a Postgres advisory lock, and exits if that fails.
- Backtests load bars through `hydra.backtest.data` from `ts.ohlcv_1m`; nothing is fetched or synthesised on the fly.

## Paper fill rules

The paper executor (`src/hydra/execution/paper_trading.py`) simulates a Binance spot account; the rules live in
`src/hydra/execution/fill_rules.py`.

- **Spot only.** A SELL needs free base, a BUY needs free USDT for cost plus fee; nothing goes negative or short.
  Resting orders lock what they may spend (a SELL locks base, a BUY locks USDT, an OCO locks once), so a second
  full-size SELL next to a stop is rejected, as on Binance. To exit while a stop rests, cancel it first or call
  `flatten()`. Optional `step_size` / `min_notional` (or per-symbol market rules) reject orders like Binance filters.
- **Fees** are charged in USDT on both sides (maker 0.1% for resting limits, taker 0.1% for market, stop and trailing
  fills by default). This is a deliberate deviation: Binance takes a BUY fee in the base asset unless paid with BNB.
- **Market orders** fill at the last closed-bar close with fixed slippage (default 0.1%).
- **Closed bars only.** `on_closed_bar(symbol, bar)` evaluates that symbol's resting orders once per bar timestamp
  (older or repeated bars are ignored), then marks the account to the close.
- **Feed gaps.** Bars replayed after a feed gap (catch-up bars) are not evaluated. A stop crossed during the gap fills
  on the first live bar, at the worse of the stop and that bar's open.
- **Intrabar path.** A bullish bar (close > open) is assumed to go open, low, high, close; any other bar open, high, low,
  close. When both OCO legs are touched, the one reached first on that path fills (the backtester's rule); a level the
  open has already gapped through is reached first.
- **Gaps.** A stop that the bar opens through fills at the open (the worse of stop and open), slipped. A resting limit
  fills at the better of limit and open, like the backtester; that is optimistic for a resting order.
- **Stop-limit.** When the trigger price is already marketable against the limit, it fills at the trigger (taker; the
  limit is only a cap). Otherwise it fills at its limit if the rest of the path reaches it, or becomes a resting limit.
  No fill rule fills outside the bar's low-high range before slippage. **Trailing stops** move their peak (or trough) point by point along the path, so a later
  high never triggers on an earlier low.
- A BUY stop that gaps above its locked amount draws the difference from free USDT, or is cancelled if there is not
  enough; `on_closed_bar` returns that cancellation as a `PaperFill` with `status="CANCELLED"`. Exits never fail on
  balance because their base is locked.
- **Halted account.** A broken ledger invariant or a second writer of the same session halts the executor: it refuses
  new orders, evaluates only resting SELLs, persists nothing more, and `flatten()` still exits.

Differences from the backtester until the P2 shared execution core: market orders fill at the last close here and at
the next bar's open there; slippage is a fixed fraction here and a square-root volume model there; the backtester fills
gap-through stops at the stop. Both pick the OCO leg with the same rule (`fill_rules.oco_stop_first`).

## Planned architecture

- **P3** splits the engine into `hydra-trader` (sessions) and `hydra-api` (REST, WebSocket, MCP).
- **MCP v1** (after P2) is served by hydra over streamable HTTP with a bearer token, on its own romulus Traefik router.
  Research and paper tools are fully available; live start, kill-switch release and live orders need a one-time code
  the user types.

## Dashboard routes

| Route | What it does today |
|-------|--------------------|
| `/` | Portfolio summary, equity and daily P&L, open positions. Shows UNAVAILABLE when the DB is down. |
| `/trading` | TradingView embed, Binance order book (REST polling, marked STALE when old), running sessions, quick trade, tri-state kill switch. |
| `/trading/[sessionId]` | Session detail: capital, P&L, positions, trades. Re-fetches on a WebSocket fill. |
| `/strategies` | Strategy list from YAML, start/stop sessions, per-strategy capital and risk overrides (overrides are not enforced yet). |
| `/builder` | Visual rule builder; preview on stored bars. Stop-loss / take-profit are not enforced. |
| `/templates` | Import a strategy from `config/templates/`. |
| `/backtest` | Run a backtest on stored bars, with a provenance panel. |
| `/backtest/[id]` | Result detail: metrics, equity curve, trades. |
| `/backtest/compare` | Compare two results, with a warning when their data differs. |
| `/optimize` | Launch hyperopt / walk-forward as an in-process task. |
| `/optimize/[taskId]` | Task progress and result. |
| `/portfolio` | Positions, allocation, balance snapshots. |
| `/risk` | Risk limits form, drawdown and daily-loss gauges, VaR; circuit breakers show UNAVAILABLE. |
| `/models` | ONNX model registry: upload, promote, rollback. Drift shows Unknown. |
| `/settings` | System config, exchange credentials and webhook secrets (write-only), notifications. |

## Project structure

```
src/hydra/
├── core/         # Config (pydantic + layered YAML), domain types, events, InMemory/Redis event bus, WS manager, Prometheus metric contract (metrics.py)
├── data/         # Ingestion (ccxt.pro), bar storage, backfill, news/sentiment; migrations/ (Alembic, the schema)
├── indicators/   # TA-Lib wrappers + custom indicators
├── strategy/     # Strategy base, engine, rule engine, YAML config; builtin/ holds the built-in classes
├── backtest/     # Runner, bar loader, fills, metrics, hyperopt, walk-forward, vectorbt_research
├── execution/    # SessionManager, OrderManager, paper executor, ccxt ExchangeClient
├── risk/         # Pre-trade checks, sizing, circuit breakers and exchange safety (not wired)
├── portfolio/    # P&L, positions, reconciliation
├── ml/           # Features, ONNX serving/export, registry, drift, training (not wired, P5)
├── dashboard/    # FastAPI app (api.py), routes/
└── ops/          # Discord / Slack webhook notifier
dashboard/        # Next.js app (see dashboard/README.md)
docker/           # engine/, dashboard/, backtest/, ml/ — one Dockerfile each
deploy/           # Local full-stack compose, Prometheus rules + promtool tests, Grafana dashboard
config/           # base.yaml, strategies/, templates/, grafana/
scripts/          # download_history.py, seed_db.py, sync_models.py
tests/            # unit/, integration/, e2e/, performance/
```

## Tech stack

- Python: CI and images run 3.14 (`.python-version`, `python:3.14-slim`); `requires-python = ">=3.12"`, ruff and mypy
  target 3.12; the ML image uses 3.12. uv is pinned to 0.13.3 in CI and images; `[tool.uv] required-version >=0.12.18`
  makes older uvs refuse to run, so they cannot rewrite `uv.lock`, while Dependabot's bundled uv still works.
- FastAPI + uvicorn, raw asyncpg (no ORM at runtime), pydantic / pydantic-settings, structlog.
- ccxt async for REST and ccxt.pro for the session market-data feed.
- TimescaleDB on PostgreSQL 16, Redis.
- Next.js 16.4, React 19, TypeScript 7 (`tsc --noEmit`; lint and `next build` use the TypeScript 6 API package),
  Tailwind CSS 4, Recharts, vitest + jsdom.
- ML libraries (onnxruntime; xgboost, torch, mlflow etc. in the `ml-training` extra) are installed in the images but
  not wired into trading (P5).
- Prometheus rules and Grafana dashboards live here; the running Prometheus, Alertmanager and Grafana are in romulus.

## Quick start

Requires [uv](https://docs.astral.sh/uv/). CI installs with:

```bash
uv sync --locked --extra dev
uv run pytest tests/unit -m "not slow"
```

`make dev` installs every extra, including `ml-training` (torch, xgboost, mlflow), which is large. `make install`
installs runtime dependencies only, without the test tools.

To run the engine against a local database, start TimescaleDB and Redis with `make dev-up` (needs Docker), then:

```bash
export DATABASE_URL=postgresql://hydra:hydra_dev@localhost:5432/hydra REDIS_URL=redis://localhost:6379/0
export HYDRA_CREDENTIAL_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
uv run python -m hydra.dashboard.api
```

Keep that key: credentials stored under it cannot be read with another one. `.env.example` lists every variable.

### Configuration

- One model, `HydraConfig` (`src/hydra/core/config.py`), loaded and validated once at startup. Precedence:
  environment > `config/{HYDRA_ENV}.yaml` > `config/base.yaml` > defaults. `HYDRA_CONFIG_DIR` selects the directory
  for `base.yaml` and the overlay only; `strategies/` and `templates/` are still read from the repo/image `config/`.
  `HYDRA_ENV` (default `base`) selects an overlay, which must exist.
- Override any key from the environment as `HYDRA_<SECTION>__<KEY>`, e.g. `HYDRA_TRADING__TESTNET=false`,
  `HYDRA_LOGGING__LEVEL=DEBUG`, `HYDRA_API__CORS_ORIGINS=https://a.example,https://b.example`.
- Infrastructure comes only from the environment: `DATABASE_URL`, `REDIS_URL`, `HYDRA_CREDENTIAL_KEY`,
  `HYDRA_API_TOKEN`. Putting them (or `database:`, `redis:`, `exchanges:`) in YAML is a startup error.
- An unknown key at any depth, an unknown or retired `HYDRA_*` variable (the message names the replacement, e.g.
  `HYDRA_CORS_ORIGINS` -> `HYDRA_API__CORS_ORIGINS`), an unresolved `${VAR}` or an invalid value stops startup with
  the key, variable or file in the message; secret values are never printed. Without `DATABASE_URL` the engine runs
  without a database (dev only).
- `HYDRA_CREDENTIAL_KEY` encrypts exchange credentials in the database and is required whenever `DATABASE_URL` is set.
  It is never generated or written to disk. Rotate with a comma list: set `new,old`, reconnect each exchange in
  Settings (re-encrypts under `new`), then drop `old`. Rows no configured key can decrypt show as undecryptable and
  must be re-entered.

### Database schema

- Alembic in `src/hydra/data/migrations` is the only schema source; never add inline DDL. Every schema change is a new
  revision on the single head (`uv run alembic revision -m "..."` with the root `alembic.ini`; CI fails on two heads).
- The engine upgrades to head at startup, under `pg_advisory_xact_lock`, in one transaction; a failure stops startup.
  By hand: `DATABASE_URL=... uv run python -m hydra.data.migrations upgrade|current|check|heads`.
- `0001` (baseline) is the 2.2.0 inline schema made idempotent, plus `system_config` with default rows. It upgrades an
  empty database and a 2.1.x/2.2.0 one in place, deletes `backtest_results` rows without data provenance (P0-B,
  irreversible) and adds no retention, compression or continuous aggregates. There is no downgrade: roll back from a
  `pg_dump`.
- `0002_paper_state` adds `paper_accounts` (one row per paper session: the full ledger as JSONB, Decimals as
  strings, plus `state_version`), `trades.order_id/fill_seq/net_pnl/liquidity/reason` with a unique
  `(session_id, fill_seq)` index, and `balance_snapshots.session_id`. `trades.pnl` stays gross; `net_pnl` is net of
  both fees. Forward-only.
- Before the first deploy of this baseline onto an existing database: take `pg_dump -Fc` and `pg_dump -s`; check
  `SELECT * FROM alembic_version` fails (no table) and `SELECT hypertable_name FROM timescaledb_information.hypertables`
  lists `ohlcv_1m`, `trades` and `funding_rates` (a plain `ts` table with rows would be converted in place); then
  restore the dump into a scratch database and run `python -m hydra.data.migrations upgrade` from the new image
  against it.

## Development

```bash
make lint          # ruff check + ruff format --check on src/ and tests/
make format        # ruff --fix + ruff format
make type-check    # mypy src/ (pyproject config; CI also runs the --strict command in ci.yml)
make security      # bandit
make test          # unit tests, not slow, with coverage
make unit          # all unit tests, verbose
make integration   # integration tests; they skip unless HYDRA_DB_* / HYDRA_REDIS_URL are set
make e2e           # e2e tests (no infrastructure needed)
make perf          # benchmarks, writes benchmark.json
make imports       # import-linter layers contract
make check         # lint + type-check + security + imports + test
make dev-up        # TimescaleDB + Redis via docker-compose.dev.yml (needs Docker)
make dev-down      # stop them
make docker-build  # build hydra-engine, hydra-dashboard, hydra-backtest (needs Docker)
make clean         # remove caches and build output
```

Other compose files:

- `deploy/compose.yml` is a local full-stack approximation, not the deployment. It needs `HYDRA_CREDENTIAL_KEY` in the
  environment; the dashboard's `/api` rewrite still does not reach the engine.
- `docker-compose.ml.yml` is non-functional until P5 (see its header).

## Testing

| Suite | Needs | Command |
|-------|-------|---------|
| Unit | nothing | `uv run pytest tests/unit -m "not slow"` |
| Integration | TimescaleDB (`HYDRA_DB_HOST`, `HYDRA_DB_PORT`, `HYDRA_DB_NAME`, `HYDRA_DB_USER`, `HYDRA_DB_PASSWORD`); Redis (`HYDRA_REDIS_URL`) | `uv run pytest tests/integration -m integration` |
| E2E | nothing yet (in-memory); CI provides TimescaleDB and Redis for the P1 paper smoke test | `uv run pytest tests/e2e -m e2e` |
| Performance | nothing | `uv run pytest tests/performance -m performance` |
| Dashboard | Node 26 | `cd dashboard && npm ci && npm test` |

The shared fixtures live in `tests/infra.py` (`fresh_database`, `database_dsn`, `redis_url`). DB tests create and drop
their own database. Tests skip when their env is unset and fail when it is set but the server is unreachable; with
`HYDRA_REQUIRE_INFRA=1` (as in CI) a skipped integration or e2e test or module fails. For the current number of tests, run
`uv run pytest --collect-only -q`.

What CI runs (`.github/workflows/ci.yml`, Python 3.14, uv 0.13.3): lint and `lint-imports`, `mypy --strict` over core,
data, indicators, strategy, backtest, execution, risk and portfolio, bandit, unit tests with a clean-tree check, the
dashboard (lint, typecheck, test, build), promtool on the Prometheus rules, integration and e2e on TimescaleDB and
Redis service containers, and an image smoke job that builds each image and starts it (the engine against the service
containers with `DATABASE_URL`, `REDIS_URL` and a throwaway `HYDRA_CREDENTIAL_KEY`). Performance tests are not run in
CI.

## CI/CD and releases

- Every PR to `main` runs CI, including the image smoke build (nothing is pushed).
- A push to `main` runs the same jobs; only when all of them pass do the publish jobs build `hydra-engine`,
  `hydra-dashboard` and `hydra-backtest` and push `main` and `sha-<7>` tags to GHCR. A push that changes none of
  `src`, `config`, `scripts`, `docker`, `dashboard`, `pyproject.toml`, `uv.lock` or `README.md` publishes nothing;
  a manual dispatch on `main` always publishes. The version tag from
  `pyproject.toml` is pushed only if it does not exist yet, so version tags never move: releasing means bumping the
  version.
- Releases happen at the end of a phase: `pyproject.toml` version, `uv lock`, and the romulus image tags move together.
- Dependabot PRs in this repo auto-merge on green CI for minor/patch updates (`dependabot-auto-merge.yml`); majors,
  0.x minors, pre-releases and base images wait for review.

## Deployment

Production is the `hydra` stack in [romulus](https://github.com/somatczk/romulus) (`stacks/hydra/`), a Docker Compose
stack on a ZimaCube running Ubuntu 26.04, applied by doco-cd. Each service there is a one-line Dockerfile
(`FROM ghcr.io/somatczk/hydra-<image>:<version>`); Dependabot in romulus opens the bump PRs. Romulus P0-E2 (in
progress) holds hydra bumps back from auto-merge, removes the public `/health` + `/metrics` router and drops the
remaining Telegram env.

## Roadmap

- **P0 Truth**: docs, UI and API stop claiming what is not true; backtests use real stored bars. (This phase.)
- **P1 Safe paper trading**: config/creds/lifespan, feed, event-bus isolation, risk and circuit-breaker wiring, paper
  executor, stops and OMS, Binance gate, API security, ops (CI on real PG/Redis, import-linter, python pin).
- **P2 Trustworthy backtester**: shared execution core, realistic fills, real WFO / CSCV PBO / DSR, durable job queue,
  real vectorbt, hot-reload.
- **MCP v1** (after P2): service layer exposed as MCP tools; live tools gated by a one-time code.
- **P3 Live spot (Binance)**: order state machine, reconciliation, exchange stops, live gating, trader/api split.
- **P4 Perps.**
- **P5 ML.**

## License

Proprietary. See [LICENSE](LICENSE).
