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
| Exchanges | Binance, Bybit, Kraken, OKX, spot + futures | **Partial: Binance SPOT only; others and perps P4.** A session for any other exchange or for FUTURES is refused at start with 400 `unsupported_venue` before anything is built, and recovery marks such rows `error` (`tests/unit/test_execution/test_session_manager.py`, `tests/unit/test_execution/test_client_factory.py`, end to end in `tests/e2e/test_paper_smoke.py::test_only_binance_paper_sessions_start`). On the session path (`src/hydra/execution`) `hydra.execution.client_factory` is the only place that builds exchange clients, always the spot class (`tests/unit/test_execution/test_session_split.py::test_only_client_factory_constructs_exchange_clients`); `routes/market_data.py` and `routes/system.py` still build their own clients (follow-ups). Live sessions are refused with 403 `live_trading_disabled` until P3. |
| Strategies | Visual rule builder + Python strategies | **Working.** Rules are saved as YAML and evaluated. Builder stop-loss / take-profit are saved in the YAML `risk:` block, read back, and enforced by paper sessions and the backtester (an OCO when a take-profit is set). A trailing stop sells the whole position, so a builder save with a trailing stop drops the take-profit the builder always sends (`take_profit_method: none`); invalid values are 422 (`tests/unit/test_dashboard/test_strategy_risk_block.py`). The builder preview runs on the latest stored bars (`test_preview_uses_the_latest_stored_bars`). |
| Strategies | Hot-reload | **Planned P2.** `StrategyEngine.reload_strategy` exists, but nothing in the app calls it; a session reads its YAML once. |
| Strategies | Built-in strategy classes and shipped configs | **Partial.** Modules in `strategy/builtin/`: breakout, composite, dca, grid, mean_reversion, ml_ensemble, momentum, rule_based, trend_following. `ml_ensemble` loads no model: it is a momentum heuristic placeholder, no shipped config uses it, and no session trades on ML (P5). DCA and Grid are refused at session start with 400 `strategy_unsupported` until P2, which rebuilds them with backtest support (`tests/unit/test_execution/test_session_manager.py::test_dca_is_refused_as_unsupported`). Shipped strategy configs all use the rule-based class; only templates use DCA and Grid. No test-generated configs are committed (`test_no_committed_test_strategy_yaml`). |
| Backtest | Runs only on stored bars, records provenance, 422 when there is no data | **Working.** `tests/integration/test_backtest_real_data.py::test_backtest_runs_on_stored_bars_with_exact_provenance`, `::test_no_stored_bars_is_422_with_available_range`, `tests/unit/test_dashboard/test_backtest_routes.py`. |
| Backtest | Results and trades persisted, identical after a restart | **Working.** `tests/integration/test_backtest_real_data.py::test_results_survive_a_restart`. Needs TimescaleDB (see [Testing](#testing)). |
| Backtest | Walk-forward / CPCV, PBO, DSR | **Planned P2.** Walk-forward does not re-optimise per fold, CPCV is not reachable from the API, PBO is not CSCV, and DSR is the mean Sharpe. |
| Backtest | VectorBT parameter sweeps | **Planned P2.** `vectorbt` is a dependency but is never imported; the sweep is a numpy SMA cross that ignores fees. |
| Backtest | Paper-session rules (decision 18) | **Working.** The backtester applies the paper rules: no entry while the symbol is held or an entry is pending, no short entry on spot, a protective exit from the strategy's `risk` block on every entry (5 % stop by default; an OCO with a take-profit; a trailing stop), gap-through stops at the worse of stop and open, and the protective order cancelled when an exit fills (`tests/unit/test_backtest/test_runner_paper_rules.py`, `tests/unit/test_execution/test_fill_rules.py`). Backtests stored before this change are not comparable with new ones. |
| Backtest | Realistic fill simulation | **Planned P2.** No partial fills, funding or liquidation; slippage is uncapped. |
| Backtest | Durable job queue | **Planned P2.** Backtests, hyperopt and walk-forward run as in-process tasks in the engine; task state is lost on restart. The `backtest-worker` image runs an idle placeholder loop. |
| ML | XGBoost training | **Planned P5.** Training code has no data loader or entrypoint. There is no LSTM code; the "LSTM Momentum" placeholder was removed in P0-B. |
| ML | ONNX inference, model upload / promote / rollback | **Planned P5.** Inference works in isolation, but no strategy calls it, nothing reads the model stage, and rollback leaves no model in Production. Upload is hardened (P1-F): the request body is counted as it arrives and cut off with 413 `model_too_large` past 200 MiB (plus 64 KiB of multipart framing), before the multipart parser spools more, chunked bodies included; the file itself is capped at 200 MiB, loaded by onnxruntime off the event loop before it is kept (422 `invalid_onnx`), written atomically, 409 `model_exists` unless `replace=true`, and a Production model is never replaced by an upload (409 `model_in_production`), also when it is promoted while the upload is being checked (`tests/unit/test_dashboard/test_models_routes.py::test_declared_length_over_the_bound_is_413_before_parsing`, `::test_chunked_body_is_counted_and_cut_off`, `::test_over_the_cap_is_413_and_writes_nothing`, `::test_production_model_is_never_replaced_by_upload`, `::test_promote_during_an_upload_is_not_overwritten`). |
| ML | Drift detection | **Planned P5.** A PSI function exists and nothing runs it. The API and UI report drift as `unknown` and retrain returns 501 (P0-C). |
| ML | ML overlay per strategy | **Planned P5.** `MLOverlayConfig` is parsed and never read. |
| Risk | 4-tier circuit breakers | **Working (paper).** `RiskService` (`src/hydra/risk/service.py`) keeps account equity (paper capital + carried PnL + running sessions at closed-bar marks), drawdown tiers at 1/5, 1/3, 2/3 and 1x `max_drawdown_pct`, the daily-loss halt latched to 00:00 UTC and the 4h loss-streak pause in `risk_state` (scope `paper`), and writes every transition, reset and kill-switch change to `risk_events`. Tier 3 calls a flatten callback (again every minute while positions remain, including after a restart), tier 4 latches the kill switch. After a restart nothing is evaluated and openings are refused until the owner calls `recovery_complete()` and every held position has a mark; a `paper_capital` change, including one made while the engine was down, goes through the audited paper reset, which rebases the ledger and leaves breakers alone. `/api/risk/circuit-breakers`, `/status`, `/live-status` and `/events` serve this state; reset needs `confirm_tier` and a reason, is audited first and is rolled back if it cannot be saved; `POST /api/risk/paper-account/reset` (`confirm: RESET`) is the operator's paper reset. Sessions feed it every closed-bar mark and every fill (stop fills included), register their sleeves at start and recovery and carry their PnL when they stop; the tier-3 callback flattens every session and tier 4 also stops them (`tests/unit/test_execution/test_flatten_and_kill_switch.py`, `tests/unit/test_risk/`, `tests/unit/test_dashboard/test_risk_truth.py`, `tests/integration/test_risk_state_pg.py`). |
| Risk | Pre-trade checks | **Working.** Orders are priced at the latest mark; qty <= 0 and openings with no mark are rejected. Reducing orders (`reduce_only`, clamped to the position) skip every entry check, so exits, stops and flattens are never blocked; an unflagged spot SELL is an opening short and is refused (`spot_short`). Openings check kill switch, breakers, position size vs sleeve (halved at tier 1), gross exposure across sessions, concurrent positions, USDT-only balance, Binance lot / min-notional, rate limit. One lock is held from the position read (clamp and open/reduce classification) through `create_order`, so two exits racing for one position fill once (`tests/unit/test_execution/test_order_manager_risk.py`). Sessions use the `RiskService`: entries are sized by `size_entry` (sleeve x min(strategy, global `max_position_pct`)) and carry their planned protective stop, so `max_risk_per_trade` applies; exits, stops and flattens are `reduce_only` (`tests/unit/test_execution/test_flatten_and_kill_switch.py::test_exit_is_not_blocked_by_the_heat_cap`). End to end, a strategy entry over the heat cap is refused and counted in `hydra_risk_rejections_total` (`tests/e2e/test_paper_smoke.py::test_risk_rejection_then_the_kill_switch_flattens_and_latches`). |
| Risk | Per-strategy risk overrides | **Known gap: unscheduled.** Not supported in P1: `PUT /api/risk/config/{strategy_id}` returns 501; `GET` returns the global limits that are enforced. |
| Risk | Stop-losses | **Paper: working; exchange-side P3.** Every paper entry gets a protective exit the moment it fills: the strategy's `risk:` block or a 5 % stop by default, an OCO when a take-profit is set, or a trailing stop. It rests in the paper ledger, fires on closed bars (a gap fills at the open), is re-armed for what is left after a partial exit, is cancelled before any other exit sells the base it locks, and is never cancelled as stale. If neither the policy stop nor the 5 % fallback can be placed, the position is closed reduce-only and a `protection_failed` WS event is sent; the failures are listed in the session detail and `health_snapshot()`. A halted ledger fails the session (error, flatten, stop) instead of taking another order (`tests/unit/test_execution/test_protective_orders.py::test_rejected_stop_and_fallback_close_the_position`, `::test_exit_on_a_halted_ledger_fails_the_session_instead_of_dropping_the_stop`). `ExchangeSafetyManager` (exchange-side stops for live) has no caller until P3. |
| Risk | Quick-trade TP / SL / trailing stop | **Working (paper).** `POST /api/trading/quick-trade` needs the target `session_id` and goes through `SessionManager.submit_manual_order`, the OrderManager and the RiskService: quantity must be > 0, a SELL only reduces the held long (422 `spot_short` otherwise), and a refused order is 422 `risk_rejected`. SL + TP become one OCO and a trailing stop is exclusive with them; they are placed reduce-only when the entry fills, also for a LIMIT entry that fills on a later bar (`exit_orders_deferred: true` until then). A resting manual LIMIT lives two primary bars; a cancelled one drops its SL/TP and sends a WS `order_cancel`. Without them the strategy's stop (5 % by default) is placed. Every entry, MARKET or LIMIT, carries its planned stop, so `max_risk_per_trade` applies (`tests/unit/test_dashboard/test_trading_routes.py`). Manual exits are MARKET orders. MARKET orders fill at the latest live price from the feed (`PriceEvent`, at most 1/s per symbol) since the last closed bar, else at that bar's close; the response carries `fill_price` and `mark_source` (`live`, `stale_live` once the feed went stale after that price, or `last_close`), as do the `order_fill` event and WS message. Sizing, the pre-trade check and resting orders still use closed-bar marks; a stop placed at a live price sees only the move from that price to the close of its bar, and breakers wait for the next closed bar after a live fill (`tests/unit/test_execution/test_live_mark.py`). |
| Risk | Kill switch | **Working.** `POST /api/trading/kill-switch` latches the flag first (in memory, then persisted; a failed write is 503 `kill_switch_not_persisted` and the latch stays), then flattens every running or errored session (an errored session without a runtime that still holds base included) and reports `positions_remaining`; a start in flight when it latches is refused; the message never claims everything is closed when positions remain. New sessions are refused while it is latched, and a latch that survives a restart flattens restored sessions instead of resuming them. Flatten orders fill at the latest live price since the last closed bar, else at its close. The `/risk` page shows `LATCHED — N positions could not be flattened` while sessions report positions left at their stop (`positions_at_stop` from `/api/trading/sessions`, read on load and every 10 s, so it survives a reload or restart and covers a tier-4 latch), the activation's flatten failures, and a 503 as latched but not persisted (`dashboard/src/app/risk/page.test.tsx`). Release (`DELETE`) needs `{"confirm": "RELEASE", "reason": ...}` and is audited (`tests/unit/test_execution/test_flatten_and_kill_switch.py`, `tests/unit/test_dashboard/test_trading_routes.py`, `tests/unit/test_execution/test_session_recovery.py::test_kill_switch_latched_across_a_restart_flattens_and_does_not_resume`). End to end: the kill switch flattens a filled paper position, persists the latch and refuses the next start with 409 `kill_switch_active` (`tests/e2e/test_paper_smoke.py::test_risk_rejection_then_the_kill_switch_flattens_and_latches`). The `/trading` and `/risk` pages send the release body. |
| Paper trading | Per-strategy capital, P&L | **Working.** Each session gets a slice of `system_config.paper_capital` (the remaining account capital by default; there is no 10000 fallback: without it nothing starts). The executor now keeps a spot ledger (no naked shorts; fees charged in USDT and carried into net P&L, entry fee pro-rata on partial exits) with equity = USDT + base x last close (`tests/unit/test_execution/test_paper_spot_ledger.py`). Sessions trade on it, and the session detail reports equity, realized net, unrealized P&L and fees from it (`tests/unit/test_execution/test_session_wiring.py`, `tests/unit/test_execution/test_session_manager.py`). Its fills carry `quantity` and `reason` (they showed quantity 0 before), also when read back from the `trades` rows after a restart, and `open_orders` lists the resting orders with type, side, price, `stop_price` (the price a stop triggers at, a trailing stop's current one; null on a LIMIT), reason and `protective`; the session list carries each session's `last_bar_at`, `entries_paused`, `persist_dirty` (null without a paper ledger) and `feed_live`, read from memory with no DB query (`tests/unit/test_dashboard/test_trading_routes.py`, `tests/unit/test_execution/test_paper_trading.py`). The paper smoke checks a full round trip to the exact fill prices, fees, net P&L and USDT balance on TimescaleDB (`tests/e2e/test_paper_smoke.py::test_round_trip_on_closed_bars_then_a_stalled_feed_fails_readiness`). |
| Paper trading | Stops, OCO and limits fill on closed bars; state survives a restart | **Working.** `PaperTradingExecutor.on_closed_bar` evaluates resting orders per symbol with the [paper fill rules](#paper-fill-rules) (`tests/unit/test_execution/test_paper_trading.py`, `test_fill_rules.py`), and every change is persisted to `paper_accounts` with trade rows idempotent on `(session_id, fill_seq)` and per-session balance snapshots; `PaperTradingExecutor.restore` rebuilds the account exactly (`tests/integration/test_paper_state_persistence.py`). `restore` also rebuilds the fill history from the session's trade rows. A commit whose acknowledgement was lost is detected and adopted; any other writer of the same session halts the executor (`PaperSessionHaltedError`). Sessions call `on_closed_bar` on every closed primary-timeframe bar before the strategy sees it and publish its fills; a halted ledger puts the session in `error` and flattens it. At startup running paper sessions are restored with `restore` and resumed with their resting stops (a held position without one gets one); a session with no saved state is marked `error` (`paper state unavailable (pre-P1 session)`), as is one whose state cannot be restored; a restored account that cannot resume (warm-up, market rules or config failure) is flattened at its restored marks and marked `error`, and live rows are stopped, never resumed. A stop or error whose status cannot be written is retried every minute and reported (`503 session_stop_not_persisted`, `persist_error`); until it lands, a restart would resume that session unless the kill switch is latched (`tests/unit/test_execution/test_session_recovery.py`, `tests/integration/test_session_recovery_pg.py`, through the real lifespan in `tests/e2e/test_paper_smoke.py::test_restart_recovers_the_position_without_a_zombie_and_stop_flattens`). |
| Paper trading | Testnet + paper by default | **Working (paper).** `trading_mode` is seeded `paper` in `system_config` (a missing or invalid row is a 503, never a silent default), and live sessions are refused until P3. Every keyed exchange call the dashboard makes goes through P1-E's read-only Binance SPOT client, on the Binance spot testnet (testnet.binance.vision) while `trading.testnet` is true (the default; `HYDRA_TRADING__TESTNET=false` for mainnet read-only). The Settings key check names that environment in its messages and stores it with `validated_at`; keys checked on the other environment show as not validated. Settings reads the balance and the open orders of `default_pair` and every running session's symbols (Binance needs a symbol per call); each panel loads on its own. A spot account has no positions: `/positions` answers 501 `spot_no_positions` without calling the exchange (`tests/unit/test_dashboard/test_system_routes.py::test_valid_keys_are_checked_then_stored_encrypted`, `::test_mainnet_when_testnet_is_off`, `::test_keys_checked_on_testnet_are_not_connected_on_mainnet`, `::test_orders_are_fetched_per_symbol`, `::test_positions_on_a_spot_account_are_501_without_a_call`). `HYDRA_TESTNET` stops startup. |
| Data | Redis Streams event bus | **Known gap: unscheduled.** P1 kept the in-process `InMemoryEventBus` per session, with subscriber failures isolated (`tests/unit/test_core/test_event_bus_memory.py`); `RedisEventBus` is used only in tests. Redis is only pinged by `/api/system/health` and is not a `/readyz` check. |
| Data | Live market data (closed bars, warm-up, gap fill, persistence) | **Working.** The session feed publishes each closed Binance spot candle once, in order, from one `watch_ohlcv_for_symbols` loop per exchange; a failing or slow subscriber neither drops the bar for others nor touches the WebSocket (`tests/unit/test_data/test_bars.py`, `tests/unit/test_data/test_ingestion.py`, `tests/unit/test_core/test_event_bus_memory.py`). Every declared timeframe reaches the strategy context once; `on_bar` runs only on the primary timeframe, and when timeframes close together the higher one is published first (`tests/unit/test_strategy/test_engine.py`). After a WebSocket error or 60 s of silence (forced reconnect) the missed closed bars are fetched over REST and delivered with `replay=True` except the newest per stream: catch-up bars are persisted and, in sessions (`publish_replay=True`; off by default for other feeds), enter the context, but only the newest is evaluated, so a signal on an older missed bar is skipped. A range REST cannot fill (outage, ban, lag) does not hold back live bars: the stream reports `stale` (`gap_unfilled`) and the range is retried in the background, then delivered as late `replay=True` bars and inserted in order in the context; a range the exchange still has no bars for after 5 answers is logged and given up. `FeedStatusEvent` reports `stale` (silence, overdue bar, consumer lag, unfilled gap) per scope and `live` per scope; entries may resume only when `feed.is_live(exchange_id)`. Warm-up REST-fills interior holes in the stored window as well as its tail, and a DB outage no longer drops bars (persistence retries until the feed stops). Live and backtest produce identical signals and indicators on the same stored bars (`tests/e2e/test_live_backtest_parity.py`), and the feed's upserts land where the backtester reads (`tests/integration/test_market_data_storage.py`). Sessions warm up from the DB (REST-filling the window) before they connect, persist live bars, publish catch-up bars (every session handler ignores `replay=True`), bound the context to the warm-up window, and pause entries while any scope is stale (`tests/unit/test_execution/test_session_wiring.py::test_feed_stale_pauses_entries_until_every_scope_is_live`); a start without enough history is 422 `insufficient_history`. The paper smoke streams committed Binance bars with intra-bar updates through a ccxt.pro-shaped fake: a forming candle that crosses the signal never trades, only the final bar does (`tests/e2e/test_paper_smoke.py::test_round_trip_on_closed_bars_then_a_stalled_feed_fails_readiness`). Higher timeframes are separate streams, not resampled (P2). Backfill and `download_history.py` no longer store the forming candle. |
| Data | PostgreSQL as source of truth | **Partial: live fills P3.** Backtest results and trades (P0-B), the platform config (row below), risk state and events, trading sessions, paper accounts, paper trades, positions and balance snapshots are persisted, and paper sessions are restored from `paper_accounts` at startup (`tests/integration/test_paper_state_persistence.py`, `tests/integration/test_session_recovery_pg.py`). Live fills are not stored (live is P3). |
| Data | Platform config persisted, secrets encrypted | **Working.** `GET/PUT /api/system/config` read and write `system_config` (one transaction, `updated_by` = the caller); the webhook secret and the Discord / Slack URLs are Fernet-encrypted at rest and write-only, and the lifespan decrypts them, so the webhook keeps its secret across a restart. A missing or invalid row is 503 `system_config_missing` / `system_config_invalid` everywhere it is read (config, portfolio), never `paper` or 10000. Every `paper_capital` save is handed to `RiskService.reload_config`, which rebases the paper ledger through its audited paper reset; a failed hand-off is 503 `risk_reload_failed` and saving the same value again retries it, and `paper_capital_pending` reports any saved value the ledger does not run on yet (`tests/unit/test_dashboard/test_system_routes.py::test_change_goes_through_the_audited_paper_reset`, `::test_retry_after_a_failed_reload_applies_the_reset`, `tests/unit/test_dashboard/test_system_config_security.py::test_webhook_secret_survives_a_restart`, `tests/integration/test_system_config_persistence.py::test_config_survives_a_restart_with_secrets_encrypted`, `::test_missing_paper_capital_row_is_503_everywhere`). |
| Data | Decimal for financial values | **Known gap: unscheduled.** Decimal in domain types and fills; floats at the exchange boundary, in routes, VaR and the flatten path. |
| Dashboard | Next.js app, routes listed under [Dashboard routes](#dashboard-routes) | **Working.** `cd dashboard && npm run build` (CI job "Dashboard Build") prints the route list. |
| Dashboard | Risk and portfolio data report unavailable / unknown instead of made-up values | **Working.** `tests/unit/test_dashboard/test_risk_truth.py`, `tests/unit/test_dashboard/test_portfolio_routes.py`, `cd dashboard && npm test`. |
| Dashboard | Every page shows an error instead of defaults | **Known gap: unscheduled.** `/`, `/portfolio` and `/trading` no longer guess `trading_mode: 'paper'`: when `/api/system/config` fails they request no `?source=` data and show the server's reason (`dashboard/src/app/page.test.tsx`, `portfolio/page.test.tsx`, `trading/page.test.tsx`). Still open: `/templates` swallows a failed fetch and shows "No templates available yet", `/portfolio` drops a failed strategy-list fetch silently, and `/api/models` serves `version: v1.0.0` and `stage: Staging` for an `.onnx` without sidecar metadata. |
| Dashboard | Portfolio summary, drawdown and daily P&L across paper sessions | **Working.** The paper account curve follows the `RiskService` ledger: the capital in force at each time (from the audited `paper_reset` events, else the ledger's capital, so a pending change moves nothing) + the sum over sessions of (last sleeve snapshot - allocation); a reset drops the result of sessions stopped before it, as the ledger drops carried P&L. It is folded in SQL to one point per hour (and per reset), `/live-metrics` reads only its 30-day window (seeded with each session's last value before it), and `/summary`'s total is the curve's last point. Max drawdown and monthly / 30-day returns are taken within reset segments, so a capital change is not a drawdown or a return. `/summary` counts realized P&L and fees of every session (running or stopped), unrealized P&L of running sessions, and daily P&L as net realized P&L since 00:00 UTC; `/daily-pnl`, `/attribution` and the Sharpe series use the same net, UTC-day definition. `/summary` reports `paper_capital_pending` and `/` and `/portfolio` show it. Position marks read the latest close of the position's own exchange, symbol and session timeframe. Bad `from_date` / `to_date` are a 422 envelope (`tests/unit/test_dashboard/test_portfolio_routes.py::test_reset_rebases_without_rewriting_history`, `::test_pending_capital_change_does_not_move_the_account`, `::test_snapshots_fold_to_one_point_per_hour`, `::test_live_metrics_twr_seeds_sessions_from_before_the_window`, `::test_daily_pnl_is_net_of_fees_per_utc_day`, `::test_two_interleaved_sleeves_are_not_an_80pct_drawdown`, `::test_daily_pnl_counts_stopped_sessions_and_nets_fees`, `::test_mark_is_the_positions_own_exchange_and_session_timeframe`, `::test_bad_date_is_422_envelope`, `tests/integration/test_system_config_persistence.py::test_portfolio_reads_on_the_real_schema`). Curve points are hourly closes, so a fall and recovery inside one hour is not in the drawdown. |
| Dashboard | Real-time WebSocket updates | **Planned P3** (reconnect, heartbeat, typed envelope). The engine serves `/ws/market`, `/ws/trades`, `/ws/portfolio`, `/ws/signals` and `/ws/risk`; only per-session trade fills are ever broadcast. The session page re-fetches its detail on a fill (P0-C), which fixed the NaN P&L. |
| Dashboard | TradingView charts | **Working (third-party data).** `/trading` shows TradingView's own Binance chart in a sandboxed cross-origin iframe, so no TradingView script runs in the dashboard origin, and production responses carry a CSP (`script-src 'self' 'unsafe-inline'`, `frame-src` TradingView only, `frame-ancestors 'none'`) (`dashboard/src/app/trading/page.test.tsx`, `dashboard/src/lib/securityHeaders.test.ts`; `cd dashboard && npm test`). The chart is not Hydra's stored bars; every other chart is Recharts. A nonce-based CSP is P2/P3. |
| Security | API authentication | **Working (engine) → romulus F-net.** Every HTTP route and WebSocket except the probes (`/livez`, `/readyz`, `/health`) and `/metrics` needs a principal: Authelia's `Remote-User`, trusted only from a TCP peer inside `api.trusted_proxy_cidrs` (uvicorn runs with `proxy_headers=False`, so `X-Forwarded-For` cannot fake the peer), or `Authorization: Bearer $HYDRA_API_TOKEN` for services. List only Traefik's static address (a /32), never the hydra-edge subnet: the subnet includes the Docker bridge gateway, which is how host processes reach the engine (startup warns about wider entries). `api.auth_mode: dev` trusts everyone and logs CRITICAL (local compose only). With no CIDRs and no token the API refuses everything (401 `auth_not_configured`). `/docs` and `/openapi.json` are protected too (`tests/unit/test_dashboard/test_auth_coverage.py`, `tests/unit/test_dashboard/test_security.py`). Production needs the romulus hydra-edge network and auth env before the P1 image bump. |
| Security | CSRF and WebSocket Origin | **Working.** Browser principals need `X-Hydra-Request: 1` on every non-GET, an allowed `Origin` (`api.cors_origins`) if one is sent, and `Sec-Fetch-Site` same-origin or none; WebSocket upgrades need an allowed `Origin`; a refused upgrade gets HTTP 401/403 with the error envelope. CORS allows only `api.cors_origins` (`tests/unit/test_dashboard/test_csrf.py`). |
| Security | Signal webhook | **Working.** `POST /api/signals/webhook` is behind API auth, disabled (503 `webhook_disabled`) until a webhook secret is set, compares the secret in constant time, targets only the named strategy's running session and sends its orders through `SessionManager.submit_manual_order` (risk-checked, 422 `risk_rejected`; a protective stop on the entry fill). An entry without `quantity` is sized like a strategy entry, a short entry is 422 `spot_short`, an exit is reduce-only and clamped to the position, and the envelope answers 409 `no_position` / 502 `execution_failed` (`tests/unit/test_dashboard/test_signals_webhook.py`). `/api/signals/telegram` was removed. |
| Monitoring | Prometheus alert rules | **Working.** `promtool test rules deploy/prometheus-rules.test.yml` (CI job "promtool check + test") has a firing and a non-firing case for every alert (`tests/unit/test_deploy/test_prometheus_rules.py::test_every_alert_has_a_firing_and_a_silent_promtool_case`), and every series a rule or panel reads is in the metric contract (`src/hydra/core/metrics.py`) and has a setter called on the runtime path, outside `hydra.ml` (`tests/unit/test_deploy/test_grafana_dashboards.py::test_every_series_a_rule_or_panel_reads_has_a_runtime_producer`); the ML inference latency alert and panels are removed until P5 for that reason. Readiness (`HydraNotReady` on `hydra_ready`), zombie sessions (`HydraSessionNotSupervised`: desired in the DB, no live task), a stalled health monitor, errored sessions (also a strategy's first error, when its labelled counter first appears at 1), the kill switch, an active breaker tier and unpersisted paper fills each alert; the health monitor sets their gauges and keeps the position and unpersisted-fill series of a stopped or errored session while it still holds base, has unpersisted fills or an incomplete flatten (`tests/unit/test_dashboard/test_health_routes.py`, `tests/integration/test_readiness.py`). Feed staleness compares the feed's timestamp gauges against Prometheus `time()` with the thresholds `/readyz` uses (`tests/unit/test_deploy/test_prometheus_rules.py::test_feed_thresholds_match_the_readiness_check`). Exchange API latency and errors are recorded for every call and outcome (`tests/unit/test_execution/test_exchange_client_metrics.py`). Drawdown, portfolio value and heat are set by the RiskService. Memory is left to romulus' container OOM and working-set rules (the 2 GiB RSS rule against an 8 GB limit was dropped). There is no reconciliation alert until P3. |
| Monitoring | Grafana dashboards (drawdown, P&L, heat, exchange errors, sessions) | **Working.** Every panel in `deploy/grafana-dashboard.json`, `config/grafana/hydra-overview.json` and `config/grafana/hydra-system.json` reads only exported series with their real labels and legends (`tests/unit/test_deploy/test_grafana_dashboards.py`). Win Rate (break-even counts as a loss) and the net-PnL-per-closing-fill heatmap are back: each paper fill is counted once and each closing fill observes its net PnL (`::test_trade_series_count_each_paper_fill_once`). The Risk row's "Circuit Breaker Tier" timeline reads the RiskService's live tier per scope (`hydra_circuit_breaker_active_tier`, `::test_tier_timeline_shows_the_live_breaker_tier`). A "Sessions & Readiness" row shows readiness by check, desired vs supervised sessions with zombies, feed message and closed-bar age against the alert limits, risk rejections, the kill switch, the engine version and the health monitor heartbeat. Rolling Sharpe, Model Accuracy, Drift PSI and ML inference latency stay removed (no daily-return series; ML is P5). |
| Monitoring | Liveness and readiness probes | **Working.** `GET /livez` is 200 `{status, version}` once startup completed and the health monitor ran within 3 intervals (45 s), else 503; the image healthcheck probes it, and `/health` is an alias until P3. `GET /readyz` is 200 or 503 with every check named: `db` (`SELECT 1`, latency), `migrations` (Alembic head), `sessions` (running in the DB vs a live task here; zombies listed), `feeds` (per supervised session: live, message age <= 120 s, closed bar <= 2 bars + 60 s), `risk` (RiskService state) and the kill switch (reported, not required). A probe gets the monitor's last result while it is at most 2 intervals (30 s) old, else one evaluation runs at a time; only the 15 s monitor run writes the gauges. Both are public and carry no positions, balances or secrets (`tests/unit/test_dashboard/test_health_routes.py`, `tests/integration/test_readiness.py`, `tests/unit/test_dashboard/test_auth_coverage.py`). Docker restarts only on `/livez`; `/readyz` drives `HydraNotReady`. The dashboard reads the same body plus `checked_at` and `stale` (older than `/readyz` would serve, on the server's clock) from `GET /api/system/readiness`, which needs credentials and only serves the monitor's last result (503 `readiness_unavailable` before the first run). |
| Monitoring | Telegram alerts | **Partial.** Paging goes through romulus Alertmanager to Telegram. The in-app bot was deleted in P0-D. |
| Engineering | mypy strict | **Partial.** CI runs `mypy --strict` over eight packages, and `pyproject.toml` relaxes strict mode for most packages through overrides. |
| Engineering | import-linter module boundaries | **Working.** `make imports` (`uv run lint-imports`) prints `Contracts: 1 kept, 0 broken.`; it runs in `make check` and in the CI "Lint & Format" job. Layers: dashboard > portfolio, backtest > execution > risk, ml > strategy > data > indicators > core, ops. |
| Engineering | CI runs unit / integration / e2e | **Working.** Integration and e2e run against TimescaleDB 2.30.2 and Redis 8 service containers with `HYDRA_REQUIRE_INFRA=1`, which turns any skipped integration or e2e test, or a tier module skipped at collection, into a failure. Tests under `tests/integration` and `tests/e2e` take their tier marker from the directory, so `-m` cannot drop one; there is no exit-code-5 escape (`tests/unit/test_repo/test_ci_gating.py::test_infra_tiers_run_on_service_containers_with_skips_as_failures`, `::test_require_infra_turns_tier_skips_into_failures`, `::test_require_infra_fails_collection_time_and_unmarked_tier_skips`). |
| Engineering | End-to-end paper smoke (P1 acceptance) | **Working.** `make smoke` (CI job "Paper Smoke") boots the real app and lifespan on TimescaleDB with romulus' env shape and token auth, seeds `ts.ohlcv_1m`, replays committed Binance BTCUSDT 1m bars (`tests/e2e/data/binance_btcusdt_1m.json`, provenance in the file) through the session seams, and checks over HTTP: an entry on a closed bar with its protective stop, the signal exit with exact prices, fees, net P&L and balance, a heat-cap rejection, the kill switch flattening and latching, the Binance-only and live gates, a restart that resumes the position and its protective stop with no zombie, and `/readyz` 503 once the feed's watchdog flags the stalled replay (`ws_silence`) and once the monitor clock passes the age limits (`tests/e2e/test_paper_smoke.py`). |
| Engineering | Images publish only after CI passes | **Working.** The publish jobs in `ci.yml` need every other job, "Paper Smoke" included, and run only on pushes to `main` and manual dispatch on `main`; a push that changes no image input publishes nothing. PRs build every image and start it in the "Image Smoke" job; the engine must answer `/livez` and `/readyz` (DB and migrations ok) with only romulus' env. Published base images are pinned by digest, so publish rebuilds on the base the smoke job ran (`tests/unit/test_repo/test_ci_gating.py::test_publish_needs_every_other_job_and_runs_only_on_main_pushes`, `::test_publish_skips_pushes_that_change_no_image_input`, `tests/unit/test_repo/test_toolchain_pins.py::test_published_images_pin_one_base_digest_per_image`). |
| Engineering | Python and uv pinned in one place | **Working.** CI runs Python 3.14 from `.python-version` and uv 0.13.3 (setup-uv, the `ghcr.io/astral-sh/uv` image in each Dockerfile; `[tool.uv] required-version >=0.12.18` refuses older uvs but admits Dependabot's); every `uv sync` is `--locked` (`tests/unit/test_repo/test_toolchain_pins.py`). |
| Engineering | JSON logs carry level, logger, timestamp and tracebacks | **Working.** Stdlib, structlog and uvicorn records, uvicorn's startup lines included, render as one JSON line each, with the traceback text; the image smoke job requires every engine and backtest log line to be JSON (`tests/unit/test_core/test_logging.py`). |
| Engineering | Every module has tests | **Known gap: unscheduled.** No tests for `backtest/vectorbt_research.py` or `ops/notifications.py`. `backtest/hyperopt.py` has no unit tests of its own; it is covered through the route tests and `tests/integration/test_backtest_real_data.py::test_hyperopt_grid_runs_on_stored_bars_reproducibly`. |
| Engineering | Dockerfile references in Makefile, compose files and CI publish job | **Working.** `tests/unit/test_repo/test_build_references.py`. |
| Engineering | One config, encrypted credentials, one schema source, fail-fast startup | **Working.** One `HydraConfig` is loaded once in the lifespan: env > `{HYDRA_ENV}.yaml` > `base.yaml` > defaults; unknown keys at any depth, unresolved `${VAR}` and infra keys in YAML stop startup (`tests/unit/test_core/test_config.py`). `HYDRA_CREDENTIAL_KEY` is required whenever `DATABASE_URL` is set and is never generated; rows it cannot decrypt are reported, not fatal (`tests/unit/test_core/test_encryption.py`, `tests/unit/test_dashboard/test_lifespan.py`). Alembic (`hydra.data.migrations`) is the only schema source and runs first under an advisory lock; a failed migration stops startup and leaves no partial schema (`tests/integration/test_migrations.py`, `tests/integration/test_startup_romulus_env.py`). `PUT /api/system/config` is persisted (P1-F). |
| Engineering | One version (pyproject) reported everywhere | **Working.** The engine package, OpenAPI, `PlatformConfig`, `/livez` and `hydra_build_info` report the pyproject version (`tests/unit/test_repo/test_version_single_source.py`), and the dashboard sidebar shows it too: `next.config.ts` reads `pyproject.toml` (the image build copies it next to the app), never `dashboard/package.json` (`dashboard/src/lib/releaseVersion.test.ts`, `cd dashboard && npm test`). |

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
        OM -->|pre-trade check| RS[RiskService<br/>one per process]
        OM --> PE[PaperTradingExecutor]
        SM --> CF[client_factory<br/>Binance spot only; live refused]
    end

    UI -->|HTTP /api, WS /ws| API
    SM -->|ccxt.pro watch_ohlcv| Binance[(Exchange)]
    CF --> Binance
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
- **Market orders** fill with fixed slippage (default 0.1%) at the latest price set by `set_mark` (the session feeds it
  every `PriceEvent`) since the last closed bar, else at that bar's close. Strategy entries run right after a closed
  bar, so they fill at its close.
- **Closed bars only.** `on_closed_bar(symbol, bar)` evaluates that symbol's resting orders once per bar timestamp
  (older or repeated bars are ignored), then marks the account to the close. An order placed at a live price sees
  its first bar as the move from that price to the close (the bar's earlier range came before the order).
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

Differences from the backtester until the P2 shared execution core: market orders fill at the last close (strategy
entries) or the live price (manual orders, flatten) here and at the next bar's open there; slippage is a fixed fraction here and a square-root volume model there. Both fill gap-through
stops at the worse of stop and open (`fill_rules.stop_trigger_price`) and pick the OCO leg with the same rule
(`fill_rules.oco_stop_first`); a gapped take-profit fills at the limit there and at the open here.

## Planned architecture

- **P3** splits the engine into `hydra-trader` (sessions) and `hydra-api` (REST, WebSocket, MCP).
- **MCP v1** (after P2) is served by hydra over streamable HTTP with a bearer token, on its own romulus Traefik router.
  Research and paper tools are fully available; live start, kill-switch release and live orders need a one-time code
  the user types.

## Dashboard routes

| Route | What it does today |
|-------|--------------------|
| `/` | Portfolio summary, equity and daily P&L, open positions. Shows UNAVAILABLE when the DB is down. |
| `/trading` | TradingView chart (cross-origin iframe), Binance order book (REST polling, marked STALE when old), running sessions, quick trade into a selected running paper session after a confirmation (PAPER, strategy, notional), kill switch confirmed both ways (release: type RELEASE and a reason). |
| `/trading/[sessionId]` | Session detail: capital, P&L, positions, trades. Re-fetches on a WebSocket fill or status change (stop, error, incomplete flatten). |
| `/strategies` | Strategy list from YAML, start/stop sessions, per-strategy capital and risk overrides (overrides are not enforced yet). |
| `/builder` | Visual rule builder; preview on stored bars. Stop-loss / take-profit are saved and enforced in paper sessions and backtests. |
| `/templates` | Import a strategy from `config/templates/`. |
| `/backtest` | Run a backtest on stored bars, with a provenance panel. |
| `/backtest/[id]` | Result detail: metrics, equity curve, trades. |
| `/backtest/compare` | Compare two results, with a warning when their data differs. |
| `/optimize` | Launch hyperopt / walk-forward as an in-process task. |
| `/optimize/[taskId]` | Task progress and result. |
| `/portfolio` | Positions, allocation, balance snapshots. |
| `/risk` | Risk limits form, drawdown and daily-loss gauges, VaR; circuit breakers show UNAVAILABLE. |
| `/models` | ONNX model registry: upload (asks before replacing a model), promote, rollback. Drift shows Unknown. |
| `/settings` | Persisted system config, exchange keys (checked read-only against the named testnet or mainnet before they are stored; shows where and when they were validated and keys the credential key cannot decrypt), balance and open orders per panel, webhook secrets (write-only), notifications. |

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
make e2e           # e2e tests (the paper smoke needs HYDRA_DB_* / HYDRA_REDIS_URL, the rest none)
make smoke         # paper smoke only: real lifespan on TimescaleDB, replayed Binance bars
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
| E2E | nothing, except the paper smoke | `uv run pytest tests/e2e -m e2e` |
| Paper smoke | TimescaleDB and Redis, as for integration | `make smoke` |
| Performance | nothing | `uv run pytest tests/performance -m performance` |
| Dashboard | Node 26 | `cd dashboard && npm ci && npm test` |

The shared fixtures live in `tests/infra.py` (`fresh_database`, `database_dsn`, `redis_url`). DB tests create and drop
their own database. Tests skip when their env is unset and fail when it is set but the server is unreachable; with
`HYDRA_REQUIRE_INFRA=1` (as in CI) a skipped integration or e2e test or module fails. For the current number of tests, run
`uv run pytest --collect-only -q`.

What CI runs (`.github/workflows/ci.yml`, Python 3.14, uv 0.13.3): lint and `lint-imports`, `mypy --strict` over core,
data, indicators, strategy, backtest, execution, risk and portfolio, bandit, unit tests with a clean-tree check, the
dashboard (lint, typecheck, test, build), promtool on the Prometheus rules, integration and e2e on TimescaleDB and
Redis service containers, the "Paper Smoke" job (`make smoke`: the P1 acceptance test, below), and an image smoke job
that builds each image and starts it (the engine against the service containers with `DATABASE_URL`, `REDIS_URL` and a
throwaway `HYDRA_CREDENTIAL_KEY`; it must answer `/livez` and `/readyz` with the DB and migrations ok). Performance
tests are not run in CI.

The paper smoke (`tests/e2e/test_paper_smoke.py`) runs the engine's real lifespan against TimescaleDB with the env
romulus sets and calls the API with the bearer token. Only the exchange is replaced: committed Binance BTCUSDT 1m bars
(`tests/e2e/data/binance_btcusdt_1m.json`) are rebased to the current minute, the warm-up part is seeded into
`ts.ohlcv_1m`, and the rest is streamed through a ccxt.pro-shaped fake with synthetic intra-bar updates
(`tests/e2e/replay_exchange.py`). Expected trades, prices, fees and balances are computed in the test from the fixture
and the paper fill model. No test calls an exchange. The fixture comes from a public mirror of Binance's klines (see
its `provenance` header). Live bars are dated from the current minute onwards, so the healthy `/readyz` cannot fail on
closed-bar age; that threshold is tested in `tests/unit/test_dashboard/test_health_routes.py` and by moving the monitor
clock. The stalled-feed case waits up to 2 minutes of wall clock for the feed watchdog's 60 s `ws_silence`.

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
