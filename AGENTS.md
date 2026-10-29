# Hydra: agent instructions

Hydra is a self-hosted crypto trading platform: rule-based and ML strategies, backtests on stored Binance bars, paper
sessions, and code-gated live trading on Binance spot and USD⌂-M perps. This file is the one set of agent
instructions for every tool (Codex, Copilot, Cursor, Gemini CLI, and Claude Code through `CLAUDE.md` → `@AGENTS.md`).
Humans start at `README.md`. What works and what does not lives only in the README **Status** table: do not
duplicate it here, and update its row in the PR that changes the behaviour.

**Local rules.** These directories have their own `AGENTS.md` (each with a sibling `CLAUDE.md` that imports it). Read
the one for the area you touch before editing; it adds to this file and wins on local detail, while the Boundaries
below always apply: `src/hydra/execution/`, `src/hydra/risk/`, `src/hydra/services/`, `src/hydra/backtest/`,
`src/hydra/strategy/`, `src/hydra/data/`, `src/hydra/ml/`, `src/hydra/dashboard/`, `dashboard/`, `tests/`, `deploy/`.

## Commands

Gates before every commit, all green:

```bash
uv sync --locked --extra dev                 # what CI installs (uv 0.13.3; an older uv refuses to run)
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
uv run mypy src/hydra/core/ src/hydra/data/ src/hydra/indicators/ src/hydra/strategy/ src/hydra/backtest/ \
  src/hydra/execution/ src/hydra/risk/ src/hydra/portfolio/ src/hydra/ml/ --strict
uv run lint-imports                          # import-linter layers contract
uv run pytest tests/unit -q -m "not slow"
git status --porcelain --untracked-files=all # must print nothing after the tests (CI enforces this)
```

- Plus every integration or e2e test you touched: `make integration`, `make e2e`, `make smoke` (they need
  `HYDRA_DB_*` and `HYDRA_REDIS_URL`; see `tests/AGENTS.md`).
- The strict mypy line is CI's. Run it whenever you touch one of those packages: `mypy src/` alone does not catch
  everything it catches.
- When anything under `dashboard/` changed:
  `cd dashboard && npm ci && npm run lint && npx tsc --noEmit && npm test && npm run build`.
- Make targets: `make lint` (ruff check + format check), `make type-check`, `make security` (bandit), `make imports`,
  `make test` (unit, not slow, with coverage), `make check` (lint + type-check + security + imports + test),
  `make docker-build` (needs Docker).

## Boundaries

**Always**
- Work in a worktree, `~/Documents/GitHub/.wt/<repo>-<ws-id>` on branch `ws/<ws-id>`, never in the main checkout.
  Do not run `tests/unit/test_dashboard` from a non-worktree checkout.
- Fix the cause of a failing check. New code comes with tests; tests assert real behaviour and real numbers at the
  seam being fixed, and do not mock the seam away.
- Read settings from `HydraConfig`, not `os.environ` (see Architecture).
- Change the schema only with a new Alembic revision on the single head (CI checks
  `python -m hydra.data.migrations heads`).

**Ask first** (these need an explicitly reviewed edit)
- A new public path: `PUBLIC_PATHS` in `src/hydra/dashboard/security.py` plus
  `tests/unit/test_dashboard/test_auth_coverage.py`. The same holds for the `MCP_PATH_PREFIX` constant.

**Never**
- Silence a check: no `noqa`, `type: ignore`, `eslint-disable`, `pytest.skip`/`xfail` or `.skip` to get green.
- Run `make dev-up` or any other Docker command unless the task explicitly owns infrastructure.
- Print `.env` contents or keys, or call exchange APIs with real keys. No test calls an exchange.
- Bump the version inside a feature PR.
- Add inline DDL. The schema comes only from Alembic revisions in `src/hydra/data/migrations/versions/`.
- Add a live trading path outside the code-gated approval executors in `src/hydra/services/live_gate.py`; every other
  surface refuses live with 409 `live_gate_required`. Arming spot never arms perps (`services/perps_gate.py`).
- Build an exchange client on the session path outside `hydra.execution.client_factory`.
- Put the hydra-edge subnet in `api.trusted_proxy_cidrs`: it lists Traefik's static /32 only (the subnet's bridge
  gateway is the host).
- Synthesise market data. Backtests run only on bars stored in `ts.ohlcv_1m`.
- Edit the MCP transport files to add a tool: tools register from `hydra.dashboard.mcp.tools`.
- Add a `Co-Authored-By` trailer, Claude or session lines, a "Generated with Claude Code" footer or session links.

## Commits, PRs, comments, releases

- **Commits**: Conventional Commits.
- **PRs**: one PR per workstream. The body says what and why, the audit IDs closed, the test evidence (commands and
  results) and any deviations from the plan.
- **Comments**: at most one short comment line where it is truly needed. Reasoning goes in the PR body.
- **Integration tests** must `pytest.fail`, not skip, when their DB env (`HYDRA_DB_*`) is set but the server is
  unreachable, and they build their own DSN. Use the `tests/infra.py` fixtures (`fresh_database`, `database_dsn`,
  `redis_url`); under `HYDRA_REQUIRE_INFRA=1` (CI) a skipped integration or e2e test fails.
- **Releases** only at the end of a phase. The version lives only in `pyproject.toml` `[project] version`; the bump,
  `uv lock` (CI's `uv sync --locked` fails on drift) and the romulus image tags for engine, dashboard and
  backtest-worker (`romulus/stacks/hydra/*/Dockerfile`, one romulus PR) move together. The app reads its version
  from `importlib.metadata.version("hydra")`; the dashboard sidebar reads `../pyproject.toml` in
  `dashboard/next.config.ts` (the image build copies it to `/pyproject.toml`); `dashboard/package.json`'s version is
  shown nowhere.

## Where things live

- `src/hydra/` has 13 packages. `core`: config, domain types, events, event buses, WebSocket manager, the Prometheus
  metric contract (`metrics.py`). `trader`: the trader process (`python -m hydra.trader [run|check]`), the engine
  behind an internal bearer-gated API over the 19 `TraderBackend` ops. `data`: ingestion, bar storage, backfill, the
  top-100 universe, on-demand fetch, news and sentiment, Alembic migrations. `indicators`: TA-Lib wrappers and custom
  indicators. `strategy`: base class, engine, rule engine, YAML config, `builtin/`. `backtest`: runner, bar loader,
  fills, metrics, hyperopt, walk-forward, job queue (`jobs.py`), worker (`worker.py`, the `hydra-backtest-worker`
  container). `execution`: SessionManager, OrderManager, paper and live executors (spot and perps), ExchangeClient,
  exchange stops, reconcile. `risk`: pre-trade checks, sizing, circuit breakers. `portfolio`: P&L, positions,
  reconciliation. `ml`: features, dataset, training, serving, export, registry, drift. `dashboard`: the FastAPI app,
  `routes/`, and `mcp/`. `services`: the operations routes and MCP tools both call (no FastAPI), including the live
  and perps gates. `ops`: Discord/Slack webhook notifier.
- `dashboard/`: the Next.js app. `config/`: `base.yaml`, `strategies/`, `templates/`. `deploy/`: local compose,
  Prometheus rules and promtool tests, Grafana dashboard. `docker/<image>/Dockerfile`: engine, dashboard, backtest,
  ml. `tests/`: `unit/`, `integration/`, `e2e/`, `performance/`. `docs/`: paper fill rules and the live-trading
  operator guide.

## Architecture (as is)

- **Processes.** One engine process holds FastAPI, the SessionManager and an `InMemoryEventBus` per session, unless
  `trader.url` (env `HYDRA_TRADER__URL`) is set: then the API process never constructs a SessionManager and binds a
  `RemoteTrader` (`hydra.dashboard.trader_client`) that calls the trader process (`python -m hydra.trader`, the same
  image: the engine behind its internal bearer-gated FastAPI on :8000 with `HYDRA_API_TOKEN`, no MCP, no CORS) one
  endpoint per `TraderBackend` op. The wire format is the shared error envelope, transport errors are 503
  `engine_unreachable`, and the DB is the only durable state they share. Redis is only health-pinged; it is not on
  the trading path.
- **Signal path.** ccxt.pro closed bar → the session's bar handler (the executor fills resting orders and marks) →
  StrategyEngine → the veto-only ML overlay on entries (when configured) → OrderManager (checked by the process-wide
  RiskService) → the session's executor: paper spot, paper perp, live spot or live perp. Backtests replay bars through
  the same paper session runtime (`src/hydra/backtest/AGENTS.md`).
- **Venues.** Binance spot (`binance`) and Binance USD⌂-M perps (`binanceusdm`) only, enforced by
  `client_factory.validate_venue`. Every paper entry gets a protective stop (strategy `risk:` block, 5 % default); live
  sessions must never hold a position without a confirmed exchange-side protective order
  (`src/hydra/execution/AGENTS.md`, `docs/live-trading.md`).
- **Live gate.** The persisted `live_armed` row is the single spot live gate, written ON only by the arm executor and
  OFF also by the ungated disarm route; perps have their own `perps_armed` row and ceremony. Gated actions go through
  one in-memory approval store with one-time 6-digit codes typed by a `user`/`dev` principal
  (`src/hydra/services/AGENTS.md`).
- **Persistence.** The engine needs a DB pool whenever `DATABASE_URL` is set; startup fails without one. Persisted:
  risk config and kill-switch flag, trading sessions, paper trades, positions and balance snapshots, backtest results
  and trades with data provenance, job state (`backtest_jobs`), exchange credentials (Fernet, `HYDRA_CREDENTIAL_KEY`),
  and `system_config` rows (a missing row is a 503, never a default; the legacy `trading_mode` row is frozen at
  `paper`). Breakers, kill switch and the paper ledger live in `risk_state` / `risk_events`. In memory only: backtest
  share-link tokens and the approval store.
- **Config.** One `HydraConfig` (`src/hydra/core/config.py`), loaded once by `src/hydra/dashboard/lifespan.py` and read
  from `app.state.config` (`hydra.dashboard.deps.get_config`). env > `{HYDRA_ENV}.yaml` > `base.yaml` > defaults;
  overrides as `HYDRA_<SECTION>__<KEY>`; unknown keys and unknown `HYDRA_*` variables fail startup. The one exception
  to "read it from the config": `src/hydra/core/encryption.py` reads `HYDRA_CREDENTIAL_KEY` (env-only by
  construction) after the lifespan has validated it.
- **API.** One pure-ASGI auth middleware (`src/hydra/dashboard/security.py`) covers every route and WebSocket except
  `PUBLIC_PATHS`; the MCP server mounts at `/mcp/` only when `HYDRA_MCP_TOKEN` is set. Errors use one envelope,
  `{"detail": {"code", "message", "state", ...}}`: services raise `hydra.services.errors.ServiceError`, routes turn it
  into the HTTP error, and the frontend reads it with `dashboard/src/lib/apiState.ts`. Reuse both
  (`src/hydra/dashboard/AGENTS.md`).
- **Probes.** `/livez` (Docker healthcheck) and `/readyz` (alerts, never restarts) serve the last result of a
  `HealthMonitor` that runs every 15 s (`src/hydra/dashboard/AGENTS.md`).

## Conventions

- Frozen dataclasses for value objects; Decimal in domain types and fills (floats remain at some boundaries).
  Pydantic for config and request validation. asyncio throughout. Type hints on new code.
- mypy: `pyproject.toml` sets strict with relaxed overrides for most packages; CI runs the strict command above.
- import-linter (`[tool.importlinter]` in `pyproject.toml`) enforces the layers, top to bottom: trader, dashboard,
  services, portfolio | backtest, execution, risk | ml, strategy, data, indicators, core | ops.
- Metrics live in `hydra.core.metrics`: every metric is declared in its `CONTRACT`, and producers only call its setters.
- Backend: FastAPI + uvicorn, raw asyncpg (no ORM; SQLAlchemy is used only by Alembic), pydantic.

## Tech stack

- **Python**: CI (`.python-version`) and images run 3.14; `requires-python = ">=3.12"` and ruff/mypy target 3.12; the
  ML image uses 3.12. uv is pinned to 0.13.3 (setup-uv, Dockerfiles); `[tool.uv] required-version >=0.12.18` refuses
  older uvs that rewrite `uv.lock` while still admitting Dependabot's bundled uv;
  `tests/unit/test_repo/test_toolchain_pins.py` keeps the pins in agreement.
- **Exchange**: ccxt async (REST) and ccxt.pro (session market-data feed).
- **Database**: TimescaleDB (PostgreSQL 16). `ts` schema for hypertables (`ts.ohlcv_1m`, `ts.trades`,
  `ts.funding_rates`), `public` for relational data.
- **Frontend**: Next.js 16.4, React 19, TypeScript 7 for `tsc --noEmit` (lint and `next build` use the TypeScript 6
  API package), Tailwind CSS 4, Recharts, vitest + jsdom. The TradingView chart on `/trading` is a cross-origin
  iframe; production responses carry a CSP (`dashboard/next.config.ts`).
- **ML**: onnxruntime, plus xgboost/scikit-learn/onnxmltools in the `ml-training` extra. Training runs as
  the `ml_train` job in the backtest worker image (Python 3.14, `--extra ml-training`). The desktop GPU stack
  (`docker/ml`, `docker-compose.ml.yml`) does not run: `hydra.ml.training` has no `__main__`.

## CI/CD

- `ci.yml` runs on PRs, pushes to `main` and manual dispatch, on Python 3.14 and uv 0.13.3: ruff and `lint-imports`,
  the strict mypy list, bandit, unit tests with a clean-tree check, the dashboard (lint, typecheck, test, build),
  promtool check + test for `deploy/prometheus-rules.yml`, integration and e2e on TimescaleDB + Redis service
  containers with `HYDRA_REQUIRE_INFRA=1`, `paper-smoke` ("Paper Smoke", `make smoke`, the P1 acceptance test), and
  `image-smoke`: each image is built without pushing and started (engine against the services with `DATABASE_URL`,
  `REDIS_URL` and a throwaway `HYDRA_CREDENTIAL_KEY`; `/livez` and `/readyz` must be green and every engine and
  backtest log line must be JSON; a committed throwaway `HYDRA_MCP_TOKEN` mounts `/mcp` and
  `.github/scripts/mcp_image_smoke.py` drives one real tool call). PR runs read the image build cache but do not
  write it.
- `publish` needs every other job and runs only on pushes to `main` and manual dispatch on `main`: it builds
  `hydra-engine`, `hydra-dashboard`, `hydra-backtest`, pushes `main` and `sha-<7>`, and pushes the version tag only if
  it does not exist yet; version tags never move. A push that changes no image input (`IMAGE_INPUTS`, which includes
  `README.md`) skips it; dispatch always publishes. Base images are pinned by digest.
  `tests/unit/test_repo/test_ci_gating.py` enforces the gating.
- `dependabot-auto-merge.yml`: Dependabot minor/patch PRs merge on green CI; majors, 0.x minors, pre-releases and
  base images wait for review.

## Deployment

romulus (the homelab infra repo) runs the `hydra` stack on a ZimaCube with Ubuntu 26.04, applied by doco-cd, from
`ghcr.io/somatczk/hydra-*` images; romulus Dependabot opens the tag-bump PRs and holds hydra bumps back from
auto-merge. It runs the split layout: the engine container with `HYDRA_TRADER__URL` serves the API and MCP, and the
trader runs beside it as the same image with `command: python -m hydra.trader`. Romulus mounts only
`config/strategies` over the image's `config/`. The engine reads infra only from `DATABASE_URL`, `REDIS_URL`,
`HYDRA_CREDENTIAL_KEY` (required with a DB; a comma list rotates) and `HYDRA_API_TOKEN`, plus behaviour overrides as
`HYDRA_<SECTION>__<KEY>`. `HYDRA_DB_*` and `HYDRA_REDIS_URL` are test-infra env only (`tests/infra.py`).
