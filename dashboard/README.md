# Hydra dashboard

Next.js 16.4 (App Router) frontend for the Hydra engine. Rules for agents and contributors are in `AGENTS.md`.

## Routes

| Route | What it does today |
|-------|--------------------|
| `/` | Portfolio summary, equity and daily P&L, open positions. Shows UNAVAILABLE when the DB is down. |
| `/trading` | Sessions: running sessions with last-bar, entries and stop-failure health, stopped sessions that still hold positions, **Start paper session** (kill switch and breaker tier shown before you start) and a confirmed per-session stop that names the positions it closes. |
| `/trading/[sessionId]` | Session detail: health (last bar, entries, stop failures, stop prices of protective orders, saved state), account numbers from the paper ledger, positions, fills, quick trade into this session (protection choice, live summary, outcome from the response), TradingView chart (cross-origin iframe) and Binance order book (REST polling, marked STALE when old). Re-fetches on a WebSocket fill or status change; otherwise polls every 15 s. |
| `/strategies` | Strategy list from YAML with per-strategy performance; enable toggle, YAML import/export, confirmed delete, Start paper session (dialog), and, for a strategy with a running session, the hot-reload state: a file-changed banner and a parameters-only reload that reports the 409 changed fields when a restart is needed, plus the entry schedule it runs under. |
| `/builder` | Visual rule builder; preview on stored bars. Stop-loss / take-profit are saved and enforced in paper sessions and backtests. A condition compares against a number or another indicator and shows its parameter bindings; the Tuned parameters panel edits the bound top-level values (kept equal to the conditions they set); the Trading schedule card sets the entry hours (UTC bar open) and weekdays and blocks a schedule no primary bar can match. |
| `/templates` | Browse the shipped templates (risk level; expected Sharpe / max drawdown / capital show NO DATA when a template sets none; a failed fetch shows an error with Retry) and import one into the strategy list, which opens it in the builder. |
| `/backtest` | Run a backtest on stored bars. The Jobs list shows the newest 50 backtest runs (state, age, progress, the worker holding it, the auto-fetch of missing bars, confirmed cancel, verbatim errors) and survives restarts; the results table compares two selected runs. |
| `/backtest/[id]` | Result detail: metrics, equity curve, trades, verify, compare, start paper session, the auto-fetch the run needed (from its job row), and a share link to copy. A link is not public — opening it needs a Hydra sign-in like every route — and tokens live in the engine's memory: a restart or redeploy ends every link and there is no revoke. |
| `/backtest/shared/[token]` | A shared result, read-only (no rename, delete, compare, start or share); a dead link says it ended with a restart. |
| `/backtest/compare` | URL-driven two-result compare (`?a=&b=`), with a warning when their data differs. |
| `/optimize` | Optimisation launcher with a Hyperopt / Walk-forward segmented control: one shared form (strategy, dates, capital, method), per-mode fields and editable parameter ranges read from the strategy (a run needs a parameter space). Both modes queue durable jobs — hyperopt progress polls with a confirmed cancel and auto-opens its results page, walk-forward renders its folds and stitched out-of-sample equity inline once its durable row completes. Recent runs lists both kinds with states, ages, the auto-fetch of missing bars and verbatim errors. Walk-forward's fold count is labelled Folds, with the train and test window lengths it implies for the chosen dates. |
| `/optimize/[taskId]` | One optimisation run by id prefix: hyperopt shows the best params, seed, train window, the holdout block and PBO / DSR / holdout PSR only when the API returned them, provenance and a trials table with per-trial status (a failed trial shows its error, never a 0.00 Sharpe); walk-forward shows the aggregate readouts, the fold table and the stitched OOS equity chart with fold-boundary labels. Both show the auto-fetch the run made before it started. Active runs poll with a confirmed cancel. |
| `/portfolio` | Positions, allocation, balance snapshots. |
| `/risk` | Risk limits form, drawdown and daily-loss gauges, VaR; circuit breakers show UNAVAILABLE. The activity log moved to `/activity`; this page keeps a Recent risk events link to it. |
| `/activity` | The merged activity trail: every risk event newest first (who did what and why, the raw kind and details one click away), filter chips All / Claude / Me / Services, rows linking to the touched session, and Claude's pending approvals at the top — each a review page (What / Why / Claude's own words, Context, Claude's recent trail) where approving needs the 6-digit one-time code Claude was given and denying needs none. Reached from the desktop rail only; on a phone via Risk and Needs you. |
| `/models` | The model registry over `ml_models`: the version list (name, version, stage, trained-at, created-by, a training-job link that loads the job's status), the honest fold metrics (OOS accuracy, base rate and mean signed return, N/A where absent — never the placeholder-era keys), the drift badge (unknown until the trader's startup measurement, then the PSI value at the 0.2/0.25 bands), typed-confirm promote/rollback carrying the reason onto the audit line, the retrain trigger (queues the bounded `ml_train` job and polls its status and its fetch of training bars to completion, with a confirmed cancel while it is queued or running) and the ONNX upload as the next Staging version. |
| `/data` | The data behind every save and backtest, read-only: the top-100 universe in rank order (24h volume, the ranking's source and freshness) and the auto-fetched bar cache, least-recently-used first (coverage, bars, estimated bytes, fetch status, last use) with totals against the `data.cache_max_gb` budget and the last sweep. Each section shows the server's reason when its read fails. Reached from the desktop rail only. |
| `/settings` | Persisted system config, exchange keys (checked read-only against the named testnet or mainnet before they are stored; shows where and when they were validated and keys the credential key cannot decrypt), balance and open orders per panel, webhook secrets (write-only), notifications. |

## Scripts

Run from `dashboard/` with Node 26 (the CI and Docker image version):

```bash
npm ci             # install from package-lock.json
npm run dev        # dev server on http://localhost:3000
npm run lint       # eslint
npm run typecheck  # tsc --noEmit (TypeScript 7)
npm test           # vitest run (jsdom)
npm run build      # next build, standalone output
npm run start      # next start
```

`npm run lint` and `next build` use the TypeScript 6 API package (`typescript` is aliased to
`@typescript/typescript6`); `typecheck` uses TypeScript 7 (`@typescript/native`).

## Talking to the engine

- `src/lib/api.ts` calls `NEXT_PUBLIC_API_URL`, or the page's own origin when it is unset. Next inlines
  `NEXT_PUBLIC_*` at build time, so only the value present during `next build` counts.
- `next.config.ts` rewrites `/api/*` to `API_URL` (default `http://localhost:8000`) under `next dev` only; a
  production build has no rewrite.
- `docker/dashboard/Dockerfile` sets neither variable, so the image calls its own origin. In production, romulus
  Traefik routes `/api` and `/ws` on the same host to the engine, which is why that works. The runtime
  `NEXT_PUBLIC_API_URL` set in romulus and in `deploy/compose.yml` does not change the bundle.
- For local development, run the engine on `localhost:8000` and use `npm run dev`; the rewrite forwards `/api`.
  WebSockets go to the page origin (`ws://localhost:3000`), so set `NEXT_PUBLIC_WS_URL=http://localhost:8000` when
  you need live WebSocket updates. Do not point `NEXT_PUBLIC_API_URL` at another port: the engine's CSRF check
  refuses `Sec-Fetch-Site: same-site` mutations, so every POST from `:3000` to `:8000` gets 403 `csrf_rejected`.

## Errors and data state

API errors use the engine's envelope `{"detail": {"code", "message", "state"}}`. Read it with
`src/lib/apiState.ts` (`stateFromError`, `describeApiError`) and show it with `src/components/ui/DataStateBadge.tsx`
(`DataStateBadge`, `DataStateErrorCard`). Do not show defaults or empty lists in place of an error.

## Build output

`next.config.ts` sets `output: "standalone"`. The Docker image copies `.next/standalone`, `.next/static` and `public/`
and runs `node server.js` on port 3000.

## Version

The sidebar shows the Hydra release version (`APP_VERSION`), which `next.config.ts` reads from the root
`pyproject.toml` (`src/lib/releaseVersion.ts`); the image build copies that file to `/pyproject.toml`, next to `/app`.
`package.json`'s own `version` is not shown.

## Tests

Tests sit next to the code as `*.test.ts` / `*.test.tsx` and run with vitest + jsdom (`vitest.config.mts`). CI runs
lint, typecheck, test and build in the "Dashboard Build" job.
