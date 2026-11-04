# Alternative data: spec

Status: draft, not started. Owner: operator. Supersedes nothing.

Hydra's strategies see only OHLCV bars and the indicators built on them. Everything else the box fetches, news
(`data/news.py`) and Fear & Greed (`data/sentiment.py`), is display-only: fetched on request, never stored as a
series, invisible to rules, ML and backtests. Only funding rates (`data/funding.py`) are stored data a strategy run
can use.

This spec adds a pipeline that turns outside data into **point-in-time features** that rules, the ML overlay and
backtests all read, then fills it source by source. The pipeline comes first: without it, every new feed is another
dashboard panel.

## Goals

1. One storage and access path for any non-bar series, so a new source is a collector plus a registry entry, not a
   schema change and a strategy-layer change.
2. **No lookahead.** A backtest bar at close time `t` sees a value only if Hydra had it by `t`: neither the event time
   nor a later revision.
3. Paper, live and backtests read features through the same code, like bars (`backtest/AGENTS.md`: no backtest-only
   paths).
4. Every source earns its place: a feature that does not improve out-of-sample results under the existing
   walk-forward and DSR tooling is switched off, and its collector removed.
5. Collector failures never touch the trading path. A dead source makes its feature stale, and stale features fail
   closed.

## Non-goals

- New venues. CFDs, CME futures and live equities are out of scope. Data for markets Hydra cannot trade (COT) is
  collected only once a venue for it exists or as a cross-asset regime input; see Phase 4.
- Synthesised or interpolated data. The root rule "never synthesise market data" covers features: a gap stays a gap.
- Execution on news: no strategy opens a position because of a headline. News is a risk filter (Phase 5).
- A general-purpose data lake. Only series that a strategy or the overlay reads.

## Architecture

### Storage (one Alembic revision, Phase 0)

```sql
CREATE TABLE IF NOT EXISTS ts.features (
    source        text             NOT NULL,  -- 'binanceusdm', 'deribit', 'fred', ...
    key           text             NOT NULL,  -- registry key, e.g. 'oi_usd'
    symbol        text             NOT NULL,  -- unified symbol, '' for market-wide series
    ts            timestamptz      NOT NULL,  -- event time: the period the value describes (its end)
    available_at  timestamptz      NOT NULL,  -- when Hydra could first have known it
    value         double precision NOT NULL,
    origin        text             NOT NULL CHECK (origin IN ('live', 'backfill')),
    PRIMARY KEY (source, key, symbol, ts, available_at)
);
-- hypertable on ts; index (source, key, symbol, available_at); compression after 7 days.

CREATE TABLE IF NOT EXISTS ts.events (
    id                 bigserial,
    source             text        NOT NULL,  -- 'rss:coindesk', 'binance_announcements', 'alpaca_news', ...
    external_id        text        NOT NULL,  -- dedupe key from the source (guid, url hash)
    published_at       timestamptz NOT NULL,
    available_at       timestamptz NOT NULL,
    title              text        NOT NULL,
    url                text,
    symbols            text[]      NOT NULL DEFAULT '{}',
    kind               text,                  -- set by the classifier: 'hack', 'delisting', 'regulatory', ...
    severity           smallint,              -- 0-3, set by the classifier
    classifier_version text,
    PRIMARY KEY (source, external_id, available_at)
);
```

- `available_at` is in the key so a **revised** value (FRED vintages, a restated ETF flow) is a new row, never an
  overwrite. Readers take, for each `ts`, the newest row with `available_at <= t`.
- **Live** rows: `available_at` = the collector's write time.
- **Backfill** rows: `available_at` = `ts + publish_lag`, where `publish_lag` is declared per feature in the
  registry. It is conservative by design: when unsure, lag more. Vintage-aware sources (ALFRED) use the real
  publication time.
- Inserts are idempotent (`ON CONFLICT DO NOTHING`), like `upsert_funding_rates`.
- The feature catalog is code, not a table (see Registry).

### Registry (`hydra.core.features`)

One frozen `FeatureDef` per feature, in `core` so `data`, `strategy`, `backtest` and `ml` can all read it without
breaking the import-linter layers:

```python
@dataclass(frozen=True)
class FeatureDef:
    ref: str              # 'binanceusdm.oi_usd'  (source.key), the name rules use
    scope: Literal["symbol", "market"]
    cadence: timedelta    # expected spacing of ts
    publish_lag: timedelta  # backfill available_at = ts + publish_lag
    max_age: timedelta    # older than this at bar close -> NaN (stale)
    venues: frozenset[str]  # which venues' symbols it describes; market-scope: every venue
    unit: str
    description: str
```

A registry test pins every ref to a collector and every collector to its refs.

### Collector process (`python -m hydra.collector`)

A third process from the same image, next to the engine and the trader: container `hydra-collector` in
`romulus/stacks/hydra`.

- **Why a separate process:** WebSockets, scraping and third-party rate limits must not share an event loop with the
  API or the trader. A crash or a ban stops one container, not trading.
- One asyncio task per source. Each task has its own `WeightBudget` (reuse `data/on_demand.py`), exponential backoff
  and a per-source enable flag.
- Writes only `ts.features` and `ts.events`. It never migrates; it waits for the Alembic head, like the backtest
  worker.
- **Symbol scope** for symbol features: the union of running sessions' symbols and the top `collectors.universe_n`
  (default 30) of the universe row, per venue. This bounds API weight and storage.
- **Health:** `python -m hydra.collector check` reads a heartbeat file, like the worker.
- **Metrics,** declared in `hydra.core.metrics` `CONTRACT`:
  - `hydra_collector_last_success_timestamp{source}`
  - `hydra_collector_rows_written_total{source}`
  - `hydra_collector_errors_total{source,kind}`
- **Alert:** `HydraCollectorStale{source}` at warning severity. A stale source is a degraded strategy input, not an
  outage; this follows the alert-noise lesson from the romulus 2026-11 triage.

### Strategy access

The strategy layer must not import `data`. Features reach it the way bars do: pushed in.

- `StrategyContext.add_feature(symbol, ref, ts, available_at, value)` and `ctx.feature(ref, symbol, timeframe)`.
  The second returns an array aligned to that stream's bars: per bar close `t`, the as-of value (newest `ts` with
  `available_at <= t`). It is NaN before the first value, NaN when the value is older than `max_age`, and NaN for
  a venue the feature does not describe.
- `add_feature` drops the indicator cache for that symbol, like `add_bar`.
- **Live and paper:** `execution/session_wiring.py` subscribes the session to the refs its strategy declares. A
  per-session poller reads new rows every `cadence/2` (min 15 s) and pushes them. Feature ingestion is decoupled
  from bars: a bar never waits for a feature.
- **Backtests:** a worker preflight next to `_funding_preflight` loads every declared ref for the job window, plus
  the warm-up and `max_age` before it. The runner pushes rows in `available_at` order, interleaved with bars.
  Hyperopt and walk-forward reuse one load, as funding does.
- **Provenance:** `DataProvenance` gains `features: {ref: {rows, live_rows, backfill_rows, first_ts, last_ts}}`.
  A backtest whose window is mostly backfill says so on the result page.

### Rule DSL

- A rule refers to a feature as an indicator named `feature:<ref>` in `indicator` or `value`, e.g.
  `"feature:binanceusdm.oi_usd"`.
- Optional transforms reuse the existing parameter syntax and `PARAM_LIMITS`:
  - `transform=pct_change,period=N`
  - `transform=zscore,period=N`
  - `transform=diff,period=N`
  - `transform=rank,period=N` (rolling percentile)
- Transforms run on the as-of-aligned array in `indicators/`, have no lookahead, and are covered by the loop-equivalence
  tests.
- `rules_required_history` counts the transform window. `indicator_registry` offers every registered ref to the
  builder under a "Data" category, with the venue filter applied.
- Parity: `dashboard/src/components/builder/bindings.ts` and `rule_based.param_bindings` treat `feature:` refs like
  any indicator, pinned by the shared fixture.
- **NaN semantics.** NaN in any operand makes a comparison false, which is the current behaviour
  (`rule_engine.py`), so:
  - A stale or missing feature fails its **condition**. In an AND group that blocks the entry (fail closed); in an
    OR group the other branches can still fire. The builder therefore marks feature conditions inside OR groups,
    and the staleness counter (see Risks) counts them either way.
  - It also blocks **exits** whose rules read it. The protective stop still guards the position (every entry has
    one), but the strategy-level exit does not fire. The builder warns when an exit rule reads a feature, and the
    docs recommend bar-based exits.
- A strategy declares its refs in a new top-level `features: [ref, ...]`. A save derives it from the rules; it is
  never hand-written.

### The event filter (risk, not signal)

A new optional strategy block, evaluated where `schedule` is (`StrategyEngine`, before publication). It therefore
gates paper, live and backtests identically:

```yaml
event_filter:
  kinds: [hack, delisting, regulatory, exchange_outage]
  min_severity: 2
  lookback: 6h        # skip entries on a symbol with a matching event in the last 6 h
  mode: enforce       # or shadow: record the would-skip, take the entry (like ml_overlay.shadow)
```

- **Entry-only**, like `schedule`. Exits, stops, flatten and the kill switch ignore it.
- A separate operator path, outside strategies, sends a Telegram alert for severity 3 on any symbol with an open
  position. It does not flatten automatically. An automatic flatten would be a new order path, and the root rules
  route every live order through the gates; propose it separately if the alerts prove reliable.

### Config and secrets

- A `HydraConfig.collectors` section, one sub-model per source: `enabled`, cadence override, and keys as
  `SecretStr`. Unknown `HYDRA_*` keys still fail startup, so each new key is a new config field.
- Env: `HYDRA_COLLECTORS__FRED__API_KEY`, `HYDRA_COLLECTORS__FINNHUB__API_KEY`,
  `HYDRA_COLLECTORS__ANTHROPIC__API_KEY`, `HYDRA_COLLECTORS__REDDIT__CLIENT_ID` / `__CLIENT_SECRET`.
- Romulus: these go into the hydra stack's 1Password item and `.doco-cd.yml` secret list; only `hydra-collector`
  maps them.
- Alpaca news reuses the stored paper key, built per call by `client_factory.build_alpaca_data_client`.

## Sources

Each phase after Phase 0 ships its collectors, registry entries, backfill (where history exists), tests and the
**evaluation** below. A phase's sources stay `enabled: false` in `base.yaml` until their evaluation passes, except
for collection: **collection starts the day a collector merges**, because several sources have no free history and
waiting only shortens the dataset.

**History** column: what can be backfilled. *Forward-only* means the data exists only from the day collection starts.

### Phase 1: crypto derivatives positioning (Binance USD⌂-M, public, no key)

| Ref | What | Cadence | History | Endpoint |
|---|---|---|---|---|
| `binanceusdm.oi_usd` | open interest, USD notional | 5m | ~30 days | `/futures/data/openInterestHist` |
| `binanceusdm.ls_ratio_accounts` | global long/short account ratio | 5m | ~30 days | `/futures/data/globalLongShortAccountRatio` |
| `binanceusdm.ls_ratio_top_positions` | top-trader long/short position ratio | 5m | ~30 days | `/futures/data/topLongShortPositionRatio` |
| `binanceusdm.taker_buy_sell` | taker buy/sell volume ratio | 5m | ~30 days | `/futures/data/takerlongshortRatio` |
| `binanceusdm.basis_pct` | (mark - index) / index | 1m | long | `/fapi/v1/premiumIndexKlines` |
| `binanceusdm.liq_long_usd`, `liq_short_usd` | liquidated notional per 5m | 5m | forward-only | WS `!forceOrder@arr` |
| `binanceusdm.book_imbalance_10` | (bid - ask) / (bid + ask) depth, top 10 levels, sampled | 1m | forward-only | WS `@depth10@500ms` |

Notes:
- The `/futures/data/*` endpoints serve about 30 days only (verify at implementation): **start this collector first.**
- The liquidation stream pushes at most one liquidation per symbol per second, so it **undercounts** during cascades.
  The feature is a lower bound; its `description` must say so.
- Order book imbalance is the heaviest collector. Limit it to running sessions' symbols, not the universe.
- Funding stays in `ts.funding_rates` (the perp executor reads it). The registry also exposes it read-only as
  `binanceusdm.funding_rate` through a view, so rules can use it without a second writer.

### Phase 2: options, stablecoins, ETF flows

| Ref | What | Cadence | History | Source |
|---|---|---|---|---|
| `deribit.dvol_btc`, `dvol_eth` | 30-day implied vol index | 1h | since 2021 | Deribit `public/get_volatility_index_data` |
| `deribit.skew_25d_btc`, `_eth` | 25-delta risk reversal, 30-day | 1h | forward-only | Deribit `public/get_book_summary_by_currency` + greeks |
| `defillama.stablecoin_supply_usd` | total stablecoin market cap | 1d | years | DefiLlama stablecoins API |
| `defillama.stablecoin_supply_chg_7d` | derived 7-day % change (transform, not stored) | n/a | n/a | n/a |
| `farside.etf_flow_btc_usd`, `_eth_usd` | US spot ETF net flow | 1d (US close) | since launch | Farside HTML (scrape) |

Notes:
- **Farside is a scrape.** Check its terms before enabling. The collector must fail loudly on layout change (a parse
  yielding zero rows is an error, not an empty day). `publish_lag` is about 1 business day, because flows are
  published the next morning and often restated, which is why `available_at` is in the key.
- These are market-scope features: every crypto symbol's rules can read them as regime inputs.

### Phase 3: macro and the event calendar

| Ref | What | Cadence | History | Source |
|---|---|---|---|---|
| `fred.vix` | VIX close | 1d | decades | FRED `VIXCLS` |
| `fred.us10y` | 10-year Treasury yield | 1d | decades | FRED `DGS10` |
| `fred.dxy_broad` | broad trade-weighted dollar | 1d | decades | FRED `DTWEXBGS` |
| `fred.fed_funds` | effective fed funds | 1d | decades | FRED `DFF` |
| `calendar.mins_to_high_impact` | minutes to the next scheduled high-impact release | 1m (derived) | n/a | see below |
| `calendar.mins_since_high_impact` | minutes since the last one | 1m (derived) | n/a | see below |

- Revised series are fetched with ALFRED vintages (`realtime_start`), so each vintage is a row whose `available_at`
  is its real publication time. This is the one source with true point-in-time history.
- **Calendar:** release dates for CPI, NFP, PCE, GDP and retail sales from FRED's release-dates API (scheduled
  dates are known in advance, so they have no lookahead), plus FOMC decision times from a checked-in
  `config/calendars/fomc.yaml` that the operator updates yearly. A test fails when the file has no entry for the
  coming 90 days.
- The calendar features are computed, not stored: a pure function of the calendar and the bar time. That keeps them
  identical in backtests.
- A simpler companion to `schedule`, the `macro_blackout: {before: 30m, after: 60m, impact: high}` block, is sugar
  over these two features for the common case.

### Phase 4: equities (Alpaca venue, backtest-only today)

| Ref / table | What | Cadence | History | Source |
|---|---|---|---|---|
| `ts.events` source `alpaca_news` | ticker-tagged headlines | stream / poll | since ~2015 | Alpaca news API (existing key) |
| `finnhub.days_to_earnings` | trading days to the next earnings date | 1d | ~1 year back | Finnhub earnings calendar (free key) |
| `sec.insider_net_buy_usd_30d` | Form 4 open-market net buying, 30-day sum | 1d | years | SEC EDGAR (requires a declared User-Agent) |
| `finra.short_interest_pct_float` | short interest / float | twice monthly | years | FINRA short interest files |
| `cftc.cot_*` | Commitments of Traders net positioning | weekly | decades | CFTC public reporting API |

**Caveat, stated up front:** Alpaca is BACKTEST ONLY in `core/venues.py`. Until a live equities venue exists, this
phase produces research, not trading. Build it only after Phases 1-3 have shown the pipeline pays.

Notes:
- Alpaca news is the one **backtestable** news source, with years of history. That makes it the training and
  evaluation set for the Phase 5 classifier, even for crypto.
- COT data is published on Friday for Tuesday's positions: `publish_lag` is 3 days plus the time to 15:30 ET. It is
  in this phase only as a cross-asset regime input; its main use (futures) has no venue.
- Earnings back-history on free tiers is shallow. `days_to_earnings` is mostly a forward-only filter.

### Phase 5: news and the event risk filter

Inputs, written to `ts.events`:
- RSS: CoinDesk, The Block, Decrypt, SEC press releases, CFTC press releases.
- Binance and Bybit announcement pages (delistings, maintenance, contract changes). This is **the highest-value
  input**: a delisting is an exit-now event for a held symbol.
- Alpaca news (Phase 4 collector).

Classifier:
- Each new event is classified once by Claude (`claude-haiku-5-5`) with structured output: `symbols`
  (validated against the registry's venues' markets, unknown symbols dropped), `kind` (closed enum), `severity` 0-3
  and a one-line rationale stored in the row.
- `classifier_version` pins the model id plus the prompt hash. A prompt or model change bumps it, and backtests
  record which version they read.
- **Cost guard:** `collectors.anthropic.max_events_per_day` and a daily token cap. Over the cap, events are stored
  unclassified and a warning metric fires. Estimate the cost from a week of raw event volume before enabling.
- **Backtest honesty:** only events classified by the same `classifier_version` count. Re-classifying history with a
  new version writes new rows with `available_at = now`; reading them for an older window is **lookahead**, so
  backtests read an explicit `classifier_version`, and the result page shows the classification date. For historical
  evaluation, classify the Alpaca news archive once per version and label the run "retro-classified".

Delivery:
- The `event_filter` strategy block, and the severity-3 open-position alert described under Architecture.
- Rollout: `mode: shadow` on every session for at least 4 weeks. Review the would-skips against actual price moves
  before any `enforce`.

### Phase 6: social (only if Phase 5 shows value)

- **Reddit:** per-ticker mention counts and the 24-hour mention velocity from a fixed subreddit list, via OAuth
  under the free tier's terms. Forward-only (Pushshift is gone). Refs: `reddit.mentions_1h`, transforms supply the
  velocity.
- **X:** deferred. The read API's useful tiers are expensive, scraping breaks its terms, and crypto X is heavily
  botted. Revisit only with a concrete hypothesis, a priced plan, and Phase 5 evidence that text signals add
  anything at Hydra's latency.
- **Decision rule:** Phase 6 starts only if a Phase 5 shadow review shows the event filter would have avoided losses
  materially larger than the entries it would have skipped.

### Phase 7: ML features

- A new `FeatureSpec` version can include registered refs, as-of joined on the 1h grid with the same
  closed-only-visible rule as the 4h and 1d joins (`ml/AGENTS.md`). The live vector and the last training row must
  still be equal.
- Drift PSI covers the new columns. A feature whose source goes stale makes the vector NaN, and serving fails closed
  as it does today.
- Only features that passed their own phase's evaluation are eligible.

## Evaluation: how a feature earns its place

For each phase, before flipping `enabled` on anything that trades:

1. **Baseline:** take an existing rule strategy that trades the phase's markets.
2. **Variant:** the same strategy plus **one** filter or condition on the new feature, chosen *before* looking at
   results and written in the PR body.
3. **Walk-forward** both variants on the same window with the existing tooling. Report OOS Sharpe, max drawdown,
   trade count, and the **DSR with the true number of variants tried** (every feature/threshold combination counts
   as a trial; this is the multiple-testing guard).
4. **Pass:** better OOS risk-adjusted return and DSR above 0.95 on at least 2 symbols. Otherwise the feature stays
   collect-only. If it fails twice with different hypotheses, the collector is removed.
5. **Short history** (forward-only sources): no walk-forward is possible until there are at least 90 days of rows.
   Until then the feature can only run in `shadow` modes.

This is deliberately strict. With dozens of candidate features, a few will look predictive by chance, and the DSR
trial count is what catches them.

## Risks

| Risk | Mitigation |
|---|---|
| Lookahead from timestamps or revisions | `available_at` in every row and in the key; as-of reads only; conservative `publish_lag`; a no-lookahead test per collector (a row is invisible to a bar that closed before its `available_at`) |
| Survivorship: the universe is today's top 100 | Backtests on delisted symbols are impossible anyway (no stored bars). Document that feature-driven results share the bar data's survivorship bias |
| Third-party terms (Farside scrape, Reddit, SEC User-Agent) | Per-source review before `enabled`; scrapes fail loudly; no paid-tier circumvention |
| Cost creep (LLM, paid APIs) | Hard daily caps in config; a metric and alert at 80 % |
| Collector load on the box | Separate container with a memory limit; per-source weight budgets; compression after 7 days. Expected storage below 1 GB/month at `universe_n=30` (verify after Phase 1) |
| Stale feature silently disabling strategies | `HydraCollectorStale` alert; a per-session "entries blocked by stale feature" counter on the dashboard |
| Overfitting through many features | The evaluation protocol: one pre-registered hypothesis per test, DSR with the honest trial count |

## Phase 0 deliverables (the foundation, before any source)

1. The Alembic revision: `ts.features`, `ts.events`, the hypertables, compression, and the funding view.
2. `hydra.core.features`: `FeatureDef` and the registry, with a test pinning refs to collectors.
3. `hydra.collector`: the process, `check`, heartbeat, budgets, metrics, and one **toy collector** in tests only
   (fixtures, no network).
4. `StrategyContext.add_feature` / `feature`, the as-of alignment, staleness, and cache invalidation.
5. Rule DSL `feature:` refs plus transforms, `required_history`, builder registry and the bindings parity fixture.
6. Session wiring (poll and push) and the backtest preflight with provenance.
7. The `event_filter` block, evaluated in `StrategyEngine` (no events yet, so a no-op).
8. Prometheus rules for `HydraCollectorStale` in `deploy/prometheus-rules.yml`, with promtool tests.
9. Romulus PR: the `hydra-collector` service (same image, `command: python -m hydra.collector`, healthcheck,
   memory limit) and its secrets in `.doco-cd.yml`.

**Acceptance tests (the seams, not mocks):**
- A row with `available_at` one second after a bar's close is invisible on that bar and visible on the next.
- A revised row supersedes the earlier vintage only from its own `available_at`.
- A stale feature makes an entry rule false. The protective stop still exits.
- Paper and backtest produce identical feature arrays on the same stored rows: a parity test like
  `test_execution_parity.py`.
- Hyperopt reuses one preflight load across trials.
- The collector dying does not change the trader's `/readyz`.

## Order of work

| Step | Ships | Why this order |
|---|---|---|
| 0 | Foundation | Everything depends on it |
| 1 | Binance positioning | Free, uses existing ccxt and weight budgeting, and its ~30-day history window means every day of delay is data lost |
| 2 | Deribit DVOL + stablecoins | Long free history, so evaluable immediately |
| 3 | FRED + calendar | Cheapest true point-in-time data; the blackout filter is useful on day one |
| 5 | Exchange announcements + classifier, shadow | Risk value even without alpha: delisting and hack alerts |
| 2b | ETF flows | After its terms check |
| 4 | Equities | Only once there is a live equities venue or a research need |
| 6 | Reddit | Only on Phase 5 evidence |
| 7 | ML features | Only for features that passed |

## Open questions for the operator

1. **Venues.** Which markets will Hydra actually trade live in the next 6 months: Binance spot and perps only, or
   also an equities venue? Phase 4's value depends on it. CFDs and CME futures are not in scope, and EU retail
   access to perps is tightening under MiCA. Confirm the account's eligibility before investing in perps-only data.
2. **LLM budget.** What is the monthly cap for the Phase 5 classifier?
3. **Forward-only sources.** Are you willing to run sources in collect-only mode for at least 90 days before they can
   affect trading?
4. **Severity-3 alerts.** Telegram (the existing ops notifier) or a separate channel, to keep trading alerts apart
   from infra alerts?
