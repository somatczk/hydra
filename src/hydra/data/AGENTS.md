# src/hydra/data

Market data (live feed, REST backfill, on-demand fetch, funding rates, the top-100 universe, the auto-cache
sweeper), bar storage in TimescaleDB and the Alembic migrations that own the whole schema. Repo-wide rules are in
the root `AGENTS.md`.

## Commands

```bash
uv run pytest tests/unit/test_data -q -m "not slow"
uv run mypy src/hydra/data/ --strict                 # CI checks this package strict
uv run python -m hydra.data.migrations heads         # must print exactly one head (CI runs it)
uv run alembic revision -m "<what>"                  # scaffold a revision (root alembic.ini, authoring only)
uv run pytest tests/integration/test_migrations.py tests/integration/test_data_sweeper.py -m integration  # HYDRA_DB_*
DATABASE_URL=... uv run python -m hydra.data.migrations upgrade|current|check
```

## Invariants

Test files without a `tests/` prefix are in `tests/unit/test_data/`.

- A revision's file and `revision` are `NNNN_<slug>`, `down_revision` is the current head, and statements are raw
  SQL through `op.execute`, idempotent (`IF [NOT] EXISTS`) like every existing revision. One head only
  (`tests/integration/test_migrations.py::test_chain_has_one_head`).
- 0001-0005 have no downgrade (restore from `pg_dump`); 0006 onward ship one.
- The engine upgrades to head at startup in one transaction under `pg_advisory_xact_lock(MIGRATION_LOCK_ID)` and
  exits if that fails (`tests/integration/test_migrations.py::test_failed_migration_aborts_startup`,
  `::test_concurrent_upgrades_from_two_processes`). There is no localhost DSN fallback (`::test_cli_has_no_localhost_fallback`).
- Schemas: `ts` holds the hypertables (`ts.ohlcv_1m`, `ts.trades`, `ts.funding_rates`); everything else is
  `public`. `ts.ohlcv_1m` stores every timeframe, keyed by `(exchange, symbol, timeframe, timestamp)`.
- Bars are closed bars only; the forming candle is never stored (`ClosedBarAssembler` in `bars.py`,
  `test_on_demand.py::test_forming_candle_is_never_stored`).
- `ts.ohlcv_1m.source` is `'feed'` (the default writer) or `'auto'` (`on_demand.py`). The sweeper deletes whole
  `'auto'` datasets only, least recently used first, until `data.cache_max_gb` fits; it recounts sizes from the bars
  and never touches a running session's symbol
  (`tests/integration/test_data_sweeper.py::test_feed_rows_survive_the_sweep_exactly`,
  `::test_a_running_sessions_symbol_survives_on_every_timeframe`).
- One dataset has one writer across processes: `ensure_available` holds a per-key asyncio lock and
  `MarketDataRepository.dataset_lock` (a Postgres advisory lock on one connection), and a caller blocked on another
  process's fetch reports `state: "waiting"` before it re-detects the gaps
  (`test_on_demand.py::test_two_processes_fetch_one_dataset_once`).
- `OnDemandFetchService` fetches exactly the missing ranges, paced by a fixed-window weight budget
  (`data.fetch_weight_per_min`), patient up to `data.fetch_patience_s`, one writer per dataset; gaps the exchange
  lacks are reported, and only zero stored bars raise `NoMarketDataError`
  (`test_on_demand.py::test_fetches_exactly_the_missing_range`, `::test_accepted_gaps_are_reported_not_raised`).
- `data` may not import `execution`: REST clients are injected by the caller (`uv run lint-imports`).
- Funding rows are what the exchange settled, inserted only when missing; an absent row means the perp executor
  skips that accrual, never an interpolated rate (`test_funding.py`).
- The universe is the top 100 Binance SPOT USDT pairs by 24 h quote volume, stablecoin, fiat and leveraged-token
  bases excluded, persisted as one `system_config` row by `services/universe.py` (`test_universe.py`).

## Where things are

- `migrations/versions/`: the schema; `migrations/runner.py`: upgrade, heads, current.
- `storage.py`: `MarketDataRepository` (bar reads and upserts, the cache ledger, `delete_auto_dataset`).
- `ingestion.py` + `bars.py`: the ccxt.pro closed-bar feed; `backfill.py` + `scripts/download_history.py`: REST gap fill.
- `on_demand.py`, `sweeper.py`, `funding.py`, `universe.py`, `errors.py` (`NoMarketDataError`, `DataFetchError`).
- `news.py`, `sentiment.py`: fetchers behind the market-data routes, not on the trading path.
