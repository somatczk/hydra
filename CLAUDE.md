# Hydra Trading Platform

## Overview
Bitcoin trading platform under active rebuild (phases P0 to P5, plus MCP after P2). Today it runs paper-trading
sessions with rule-based strategies on Binance market data, backtests on bars stored in TimescaleDB, and live
sessions behind a code-gated arming ceremony (P3-C: the persisted `live_armed` row, per-session and per-order
one-time codes, trade-only Binance keys). What works and what does not is kept in one place, the **Status**
table in `README.md`. Do not duplicate it here; update that table in the PR that changes a row.

## Working rules (every agent session in this repo)
- **Commits**: Conventional Commits. No `Co-Authored-By` trailer, no Claude or session lines.
- **PRs**: no "Generated with Claude Code" footer and no session links. The body says what and why, the audit IDs
  closed, the test evidence (commands and results) and any deviations from the plan. One PR per workstream.
- **Gates before every commit**, all green:
  - Python: `uv sync --locked --extra dev`, then `uv run ruff check .`, `uv run ruff format --check .`,
    `uv run mypy src/`, `uv run pytest tests/unit -q -m "not slow"`, plus any integration or e2e tests you touched.
  - Dashboard, when anything under `dashboard/` changed:
    `cd dashboard && npm ci && npm run lint && npx tsc --noEmit && npm test && npm run build`.
  - `git status --porcelain --untracked-files=all` must be clean after the tests (CI enforces this).
- **Never silence a check**: no `noqa`, `type: ignore`, `eslint-disable`, `pytest.skip`/`xfail` or `.skip` to get
  green. Fix the cause.
- **Comments**: at most one short comment line where it is truly needed. Reasoning goes in the PR body.
- **Tests** assert real behaviour and real numbers at the seam being fixed; do not mock the seam away.
- **Integration tests** must `pytest.fail`, not skip, when their DB env (`HYDRA_DB_*`) is set but the server is
  unreachable, and they build their own DSN. Use the `tests/infra.py` fixtures (`fresh_database`, `database_dsn`,
  `redis_url`); under `HYDRA_REQUIRE_INFRA=1` (CI) a skipped integration or e2e test fails.
- **Releases** only at the end of a phase: the `pyproject.toml` version, `uv lock` and the romulus image tags move
  together. Never bump the version inside a feature PR.
- **Worktrees**: work in `~/Documents/GitHub/.wt/<repo>-<ws-id>` on branch `ws/<ws-id>`, never in the main checkout.
  Do not run `tests/unit/test_dashboard` from a non-worktree checkout.
- **Infra**: do not run `make dev-up` or other Docker commands unless the task explicitly owns infrastructure.
- **Secrets**: never print `.env` contents or keys. Never call exchange APIs with real keys.

## Tech stack
- **Python**: CI (`.python-version`) and images run 3.14; `requires-python = ">=3.12"` and ruff/mypy target 3.12; the
  ML image uses 3.12. uv is pinned to 0.13.3 (setup-uv, Dockerfiles); `[tool.uv] required-version >=0.12.18` refuses
  older uvs that rewrite `uv.lock` while still admitting Dependabot's bundled uv;
  `tests/unit/test_repo/test_toolchain_pins.py` keeps the pins in agreement.
- **Backend**: FastAPI + uvicorn, raw asyncpg (no ORM), pydantic. The schema comes only from Alembic revisions in
  `src/hydra/data/migrations/versions/` (SQLAlchemy is used only by Alembic); never add inline DDL. A schema change is
  a new revision on the single head (CI checks `python -m hydra.data.migrations heads`).
- **Config**: one `HydraConfig` (`src/hydra/core/config.py`), loaded once by `src/hydra/dashboard/lifespan.py` and read
  from `app.state.config` (`hydra.dashboard.deps.get_config`). env > `{HYDRA_ENV}.yaml` > `base.yaml` > defaults;
  overrides as `HYDRA_<SECTION>__<KEY>`; unknown keys and unknown `HYDRA_*` variables fail. New code reads settings
  from it, not from `os.environ`. The one exception is `src/hydra/core/encryption.py`, which reads
  `HYDRA_CREDENTIAL_KEY` (env-only by construction) after the lifespan has validated it.
- **Exchange**: ccxt async (REST) and ccxt.pro (session market-data feed).
- **Database**: TimescaleDB (PostgreSQL 16). `ts` schema for hypertables (`ts.ohlcv_1m`, `ts.funding_rates`),
  `public` for relational data.
- **Frontend**: Next.js 16.4, React 19, TypeScript 7 for `tsc --noEmit` (lint and `next build` use the TypeScript 6
  API package), Tailwind CSS 4, Recharts, vitest + jsdom. The TradingView chart on `/trading` is a cross-origin
  iframe; production responses carry a CSP (`dashboard/next.config.ts`).
- **ML**: onnxruntime, plus xgboost/torch/mlflow in the `ml-training` extra. Not wired into trading (P5).

## Project structure
- `src/hydra/`: 13 modules.
  - `core`: config, domain types, events, event buses, WebSocket manager, the Prometheus metric contract
    (`metrics.py`).
  - `trader`: the trader process entry point (`python -m hydra.trader [run|check]`): the worker-pattern
    startup plus the internal bearer-gated API over the 19 `TraderBackend` ops (D5).
  - `data`: ingestion, bar storage, backfill, news and sentiment.
  - `indicators`: TA-Lib wrappers and custom indicators.
  - `strategy`: base class, engine, rule engine, YAML config, `builtin/`.
  - `backtest`: runner, bar loader, fills, metrics, hyperopt, walk-forward, job queue (`jobs.py`),
    worker (`worker.py`, the `hydra-backtest-worker` container).
  - `execution`: SessionManager, OrderManager, paper executor, ExchangeClient.
  - `risk`: pre-trade checks, sizing, circuit breakers.
  - `portfolio`: P&L, positions, reconciliation.
  - `ml`: features, serving, export, registry, drift, training.
  - `dashboard`: the FastAPI app, `routes/`, and `mcp/` (the MCP server, mounted at `/mcp/`
    only when `HYDRA_MCP_TOKEN` is set).
  - `services`: backtest, research and strategy operations the routes (and later MCP) call; no FastAPI.
    `services/sessions.py` is the session/kill-switch/quick-trade seam both `routes/trading.py` and the
    MCP session/risk tools call; the routes keep only their models, audit lines and principals.
    `services/live_gate.py` is the live gate: the `live_armed` row, trade-only credential checks
    (the R8 live re-fetch) and the four code-gated live executors `routes/live.py` and the MCP live
    tools issue challenges against.
  - `ops`: Discord/Slack webhook notifier.
- `dashboard/`: Next.js app (see `dashboard/README.md`).
- `config/`: `base.yaml`, `strategies/`, `templates/`.
- `deploy/`: local compose, Prometheus rules and promtool tests, Grafana dashboard.
- `docker/<image>/Dockerfile`: engine, dashboard, backtest, ml.
- `tests/`: `unit/`, `integration/`, `e2e/`, `performance/`.

## Commands
```bash
uv sync --locked --extra dev   # what CI installs (uv 0.13.3; an older uv refuses to run)
make lint                      # ruff check + format check (src/ tests/)
make type-check                # mypy src/
make security                  # bandit
make test                      # unit tests, not slow, with coverage
make e2e                       # e2e tests (the paper smoke needs HYDRA_DB_* and HYDRA_REDIS_URL)
make smoke                     # paper smoke: real lifespan on TimescaleDB, replayed Binance bars
make integration               # integration tests; need HYDRA_DB_* and HYDRA_REDIS_URL
make check                     # lint + type-check + security + imports + test
make imports                   # import-linter layers contract (also in CI lint)
make docker-build              # needs Docker
```
CI also runs `uv run mypy src/hydra/core/ src/hydra/data/ src/hydra/indicators/ src/hydra/strategy/
src/hydra/backtest/ src/hydra/execution/ src/hydra/risk/ src/hydra/portfolio/ --strict`. Run it when you touch
those packages, because `mypy src/` alone does not catch everything it catches.

## Architecture (as is)
- One engine process holds FastAPI, the SessionManager and an `InMemoryEventBus` per session, **unless
  `trader.url` (env `HYDRA_TRADER__URL`) is set (P3-D5)**: then the dashboard/API process never constructs a
  SessionManager and binds a `RemoteTrader` (`hydra.dashboard.trader_client`) that calls one endpoint per
  `TraderBackend` op on the trader process (`python -m hydra.trader`: the engine behind its internal
  bearer-gated FastAPI on :8000 with `HYDRA_API_TOKEN`, no MCP, no CORS; the wire format is the shared
  error envelope; transport errors are 503 `engine_unreachable`; the DB is the only durable shared state).
  Redis is only health-pinged; it is not on the trading path.
- Signal path: ccxt.pro closed bar → the session's bar handler (paper executor fills resting orders and marks) →
  StrategyEngine → OrderManager (check by the process-wide RiskService) → PaperTradingExecutor. Every exchange client is
  built by `hydra.execution.client_factory` (Binance spot only); live sessions are code-gated — the four
  ApprovalStore executors in `services/live_gate.py` (`live_arming`, `live_session_start`, `live_order`,
  `credential_delete`) are the only live paths, and every API surface refuses live 409 `live_gate_required`.
- Persisted (the engine has a DB pool whenever `DATABASE_URL` is set; startup fails without one): risk config and
  kill-switch flag, trading sessions, paper trades, positions and balance snapshots, backtest results and trades with
  data provenance, backtest/hyperopt/walk-forward job state (`backtest_jobs`, run by the `hydra-backtest-worker`
  container: durable across engine and worker restarts, requeued up to 3 attempts, cancellable, 6 h timeout, heartbeat
  file + Docker healthcheck), exchange credentials (Fernet, `HYDRA_CREDENTIAL_KEY`; `validated_permissions` is the
  connect-time Binance apiRestrictions record), and `system_config` rows (`PUT /api/system/config` writes them,
  secrets Fernet-encrypted; a missing row is a 503, never a default; a `paper_capital` change goes through
  `RiskService.reload_config`; the legacy `trading_mode` row is frozen at `paper` and unwritable — the `live_armed`
  row is the single live gate, written ON only by the arm executor and OFF also by the ungated disarm route, toward
  safety, audited). In memory only: the backtest share-link tokens and
  the approval store. Breakers, kill switch and the paper account ledger live in `risk_state` / `risk_events`;
  paper sessions are restored from `paper_accounts` by `SessionManager.startup()`.
- Gated MCP actions go through one in-memory approval store (`services/approvals.py`, served by
  `routes/activity.py` on `app.state`): at most 20 pending, one-time 6-digit codes with a
  10-minute expiry and 5 wrong attempts, a second request for the same action returns the
  pending approval (a different same-action request answers 409 `approval_conflict`), and
  every lifecycle transition is a best-effort `risk_events` `approval_*` row plus a `hydra.audit`
  line (nothing persists across a restart). Only `user`/`dev` principals can decide one;
  `requested_by` attributes the executed action to whoever asked (`service:mcp` from the tools,
  the signed-in principal from `/api/live`). Executors run in the API process through the
  `TraderBackend` and re-validate their target state at execution time; a `ServiceError` they
  raise reaches the deciding caller unchanged (R22), anything else answers 503
  `approval_execution_failed`.
- Every paper entry gets a protective stop (strategy `risk:` block, 5 % default). Exchange-side stop placement for
  Binance spot exists (`execution/exchange_stops.py`: the OCO order-list + fallback ladder, `normalize_open_orders`,
  the A8 verify and the dead-man gauge/alert); live sessions place real exchange stops once P3-A lands the live
  executor. Only Binance SPOT is accepted (`client_factory.validate_venue`).
- Probes (`src/hydra/dashboard/health.py`, `routes/health.py`): a `HealthMonitor` started last by the lifespan runs
  every 15 s and sets `hydra_ready{check}`, the per-session desired/supervised gauges and its heartbeat; `/readyz`
  serves its last result while it is at most 30 s old and never writes gauges. `/livez`
  (Docker healthcheck) needs startup done and a recent monitor run; `/readyz` (alerts,
  never restarts) needs the DB, the Alembic head, no zombie session (running in the DB without a live task), fresh
  feeds for supervised sessions and the RiskService. Session health comes from `SessionManager.health_snapshot()`.
- API auth lives in one pure-ASGI middleware, `src/hydra/dashboard/security.py`: every route and WebSocket except
  `PUBLIC_PATHS` (`/livez`, `/readyz`, `/metrics`) needs a principal (Authelia `Remote-User` from a peer in
  `api.trusted_proxy_cidrs`, bearer `HYDRA_API_TOKEN`, or anyone in `api.auth_mode: dev`), and browser principals
  must send `X-Hydra-Request: 1` on non-GET requests. `api.trusted_proxy_cidrs` lists Traefik's static /32, never
  the hydra-edge subnet (its bridge gateway is the host). Routes need no auth code; read the caller with
  `current_principal` and log security actions with `audit()`. A new public path needs a reviewed edit to
  `PUBLIC_PATHS` and `tests/unit/test_dashboard/test_auth_coverage.py`. Dashboard unit tests call the real app as
  the bearer-token service (`default_api_auth` in their conftest); auth tests override that fixture.
- The MCP server (D5) is `hydra.dashboard.mcp`: one shared `MCPServer('hydra')` instance whose
  streamable-HTTP app the lifespan mounts at `/mcp/` **only** when the env-only `HYDRA_MCP_TOKEN`
  (≥32 chars; must differ from `HYDRA_API_TOKEN` and requires `mcp.allowed_hosts`) is set — no
  token, no surface (`/mcp` 404s). `SecurityMiddleware` deliberately skips `/mcp` and `/mcp/...`
  (`MCP_PATH_PREFIX`, a reviewed constant; `PUBLIC_PATHS` is unchanged): the mount's own pure-ASGI
  gate does the constant-time bearer check and binds `Principal('service', 'mcp')`. MCP tools
  register on the shared instance from `hydra.dashboard.mcp.tools` (`tools/list` is empty until
  MCP-B1/B2 fill the placeholder modules — never edit the transport files for a tool).
- Errors from dashboard routes use one envelope, `{"detail": {"code", "message", "state", ...}}`, built by
  `src/hydra/dashboard/routes/_db.py`; services raise `hydra.services.errors.ServiceError` with the same detail and
  `ServiceErrorRoute` turns it into the HTTP error. The frontend reads it with `dashboard/src/lib/apiState.ts`.
  Reuse both.
- Backtests run only on stored bars (`hydra.backtest.data`); no data means 422, never a synthetic fallback.
  `BacktestRunner` publishes them on a paper `SessionRuntime` bus (in-memory RiskService on bar time, drawdown tiers
  off, metrics muted), so backtest and paper share one execution path; `test_execution_parity.py` keeps them equal.
- The app version comes from `importlib.metadata.version("hydra")`, so `pyproject.toml` is the single source.

## Code conventions
- Frozen dataclasses for value objects; Decimal in domain types and fills (floats remain at some boundaries).
- Pydantic for config and request validation. asyncio throughout.
- Type hints on new code. mypy: `pyproject.toml` sets strict with relaxed overrides for most packages; CI runs the
  strict command above.
- import-linter (`[tool.importlinter]`) enforces the layers in `make check` and CI. Metrics live in `hydra.core.metrics`;
  every metric is declared in its `CONTRACT`, and producers only call its setters.
- New code comes with tests. Not every existing module has them.

## Versioning and release
- The version lives only in `pyproject.toml` `[project] version`. After changing it, run `uv lock` so `uv.lock`
  matches; CI uses `uv sync --locked` and fails on drift.
- Bumps happen only at phase end. The same release moves the romulus image tags for engine, dashboard and
  backtest-worker (`romulus/stacks/hydra/*/Dockerfile`) in one romulus PR.
- The dashboard sidebar shows the pyproject version: `dashboard/next.config.ts` reads `../pyproject.toml` (the image
  build copies it to `/pyproject.toml`). `dashboard/package.json`'s own version is not shown anywhere.

## CI/CD
- `ci.yml` on PRs, pushes to `main` and manual dispatch, on Python 3.14 and uv 0.13.3: ruff and `lint-imports`, strict
  mypy over eight packages, bandit, unit tests with a clean-tree check, dashboard (lint, typecheck, test, build),
  promtool check + test for `deploy/prometheus-rules.yml`, integration and e2e on TimescaleDB + Redis service
  containers with `HYDRA_REQUIRE_INFRA=1`, `paper-smoke` ("Paper Smoke", `make smoke`, the P1 acceptance test on the
  same services), and `image-smoke`, which builds each image without pushing and starts it (engine against the
  services with `DATABASE_URL`, `REDIS_URL` and a throwaway `HYDRA_CREDENTIAL_KEY`; `/livez` and `/readyz` must be
  green and every engine and backtest log line must be JSON; a committed throwaway `HYDRA_MCP_TOKEN` mounts `/mcp` and
  `.github/scripts/mcp_image_smoke.py` drives one real `session_list` tool call through the served `python -m` layout).
  PR runs read the image build cache but do not write it.
- `publish` (in `ci.yml`) needs every other job and runs only on pushes to `main` and manual dispatch on `main`: it
  builds `hydra-engine`, `hydra-dashboard`, `hydra-backtest`, pushes `main` and `sha-<7>`, and pushes the version tag
  only if it does not exist yet. Version tags never move. A push that changes no image input (`IMAGE_INPUTS`) skips
  it; dispatch always publishes. Published base images are pinned by digest (Dependabot bumps them).
  `tests/unit/test_repo/test_ci_gating.py` enforces the gating.
- `dependabot-auto-merge.yml`: Dependabot minor/patch PRs merge on green CI; majors, 0.x minors, pre-releases and base
  images wait for review.

## Deployment
romulus (homelab infra repo) runs the `hydra` stack on a ZimaCube with Ubuntu 26.04, applied by doco-cd. Images come
from `ghcr.io/somatczk/hydra-*`; romulus Dependabot opens the tag-bump PRs. Romulus P0-E2 (in progress) holds hydra
bumps back from auto-merge, drops the public `/health` + `/metrics` router and removes the Telegram env. The engine
reads infra only from `DATABASE_URL`, `REDIS_URL`, `HYDRA_CREDENTIAL_KEY` (required with a DB; comma list = rotation)
and `HYDRA_API_TOKEN`, and behaviour overrides as `HYDRA_<SECTION>__<KEY>` (e.g. `HYDRA_API__CORS_ORIGINS`;
`HYDRA_TRADER__URL` turns the engine container into the split API — the trader runs beside it as the same
image with `command: python -m hydra.trader`, D6). Until the
P1 release romulus bind-mounts its own `/app/config` over the image's; from then on it mounts only
`config/strategies`. `HYDRA_DB_*` and `HYDRA_REDIS_URL` are test-infra env only (`tests/infra.py`).
