'use client';

import { useEffect, useState } from 'react';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { Table } from '@/components/ui/Table';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { Section } from '@/components/ui/Section';
import { usePoll } from '@/hooks/usePoll';
import { age, utc } from '@/lib/format';
import { rankedAtPhrase, type UniverseMember, type UniverseSnapshot } from '@/components/builder/UniversePicker';

/* ---------- Types (the market_data routes' own shapes) ---------- */

/** `GET /api/market/data-inventory`: one auto-cached dataset, LRU-first. */
interface CacheDataset {
  exchange: string;
  symbol: string;
  timeframe: string;
  coverage: { first_ts: string; last_ts: string };
  rows: number;
  estimated_bytes: number;
  status: string;
  last_used_at: string;
}

interface CacheTotals {
  bytes: number;
  rows: number;
  pairs: number;
  budget_bytes: number;
  last_sweep: string | null;
}

interface DataInventory {
  datasets: CacheDataset[];
  totals: CacheTotals;
  sweep_interval_s: number;
}

/* ---------- Helpers ---------- */

/** 1024-divisor bytes, 1 dp — the same display the models page uses for file sizes. */
function fmtBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

const VOLUME = new Intl.NumberFormat('en-GB', { notation: 'compact', maximumFractionDigits: 1 });

/** The ranking is refreshed daily with hourly retries; two missed days mean something is wrong. */
const UNIVERSE_STALE_H = 48;

/* ---------- Page ---------- */

export default function DataPage() {
  const universe = usePoll<UniverseSnapshot>('/api/market/universe', 60_000);
  const inventory = usePoll<DataInventory>('/api/market/data-inventory', 60_000);

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold text-ink-1">Data</h1>
        <p className="text-sm text-ink-2 mt-1">
          The tradable universe behind every save, and the auto-fetched bar cache behind every backtest
        </p>
      </div>

      <Section title="Top-100 universe">
        {universe.state === 'loading' && universe.data === null ? (
          <Table
            columns={universeColumns()}
            data={[]}
            keyExtractor={(m) => m.symbol}
            loading
            emptyMessage="No members"
          />
        ) : universe.data === null ? (
          <DataStateErrorCard
            error={universe.error}
            fallback="Universe unavailable"
            onRetry={() => void universe.refetch()}
          />
        ) : (
          <>
            <p className="mb-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-2">
              <DataStateBadge
                state={universe.data.age_hours > UNIVERSE_STALE_H ? 'stale' : 'ok'}
                reason={rankedAtPhrase(universe.data.age_hours, universe.data.ranked_at)}
              />
              <span>
                {universe.data.count} {universe.data.exchange_id} {universe.data.quote_asset} pairs by
                rolling-24h quote volume · {rankedAtPhrase(universe.data.age_hours, universe.data.ranked_at)}
              </span>
            </p>
            <p className="mb-3 text-xs text-ink-2">Source: {universe.data.source}</p>
            <Table
              columns={universeColumns()}
              data={universe.data.symbols}
              keyExtractor={(m) => m.symbol}
              emptyMessage="The stored ranking lists no members"
            />
            <p className="mt-3 text-xs text-ink-2">
              Builder saves and the default pair must be current members; a pair that drops out keeps its
              running sessions and stored strategies — it only blocks new saves until it re-enters.
            </p>
          </>
        )}
      </Section>

      <Section title="Auto-fetched cache">
        {inventory.state === 'loading' && inventory.data === null ? (
          <Table
            columns={inventoryColumns()}
            data={[]}
            keyExtractor={(d) => `${d.exchange}:${d.symbol}:${d.timeframe}`}
            loading
            emptyMessage="No datasets"
          />
        ) : inventory.data === null ? (
          <DataStateErrorCard
            error={inventory.error}
            fallback="Cache inventory unavailable"
            onRetry={() => void inventory.refetch()}
          />
        ) : (
          <Inventory inventory={inventory.data} />
        )}
      </Section>
    </div>
  );
}

function universeColumns() {
  return [
    {
      key: 'rank',
      header: 'Rank',
      numeric: true,
      render: (m: UniverseMember) => <span className="tabular-nums">#{m.rank}</span>,
    },
    { key: 'symbol', header: 'Symbol' },
    {
      key: 'quote_volume',
      header: '24h volume',
      numeric: true,
      render: (m: UniverseMember) => (
        <span className="tabular-nums">{VOLUME.format(m.quote_volume)}</span>
      ),
    },
  ];
}

function inventoryColumns() {
  return [
    {
      key: 'symbol',
      header: 'Market',
      render: (d: CacheDataset) => (
        <span>
          {d.symbol} <span className="text-ink-2">· {d.exchange} · {d.timeframe}</span>
        </span>
      ),
    },
    {
      key: 'coverage',
      header: 'Coverage (requested)',
      hideOnMobile: true,
      render: (d: CacheDataset) => (
        <span className="tabular-nums">
          {utc(d.coverage.first_ts) ?? d.coverage.first_ts} → {utc(d.coverage.last_ts) ?? d.coverage.last_ts}
        </span>
      ),
    },
    {
      key: 'rows',
      header: 'Bars',
      numeric: true,
      render: (d: CacheDataset) => (
        <span className="tabular-nums">{d.rows === 0 ? '0 (no bars)' : d.rows.toLocaleString('en-GB')}</span>
      ),
    },
    {
      key: 'estimated_bytes',
      header: 'Est. size',
      numeric: true,
      hideOnMobile: true,
      render: (d: CacheDataset) => <span className="tabular-nums">{fmtBytes(d.estimated_bytes)}</span>,
    },
    {
      key: 'status',
      header: 'Fetch',
      render: (d: CacheDataset) => (
        <StatusBadge
          status={d.status === 'ready' ? 'READY' : d.status.toUpperCase()}
          variant={d.status === 'ready' ? 'success' : 'info'}
          size="sm"
        />
      ),
    },
    {
      key: 'last_used_at',
      header: 'Last used',
      hideOnMobile: true,
      render: (d: CacheDataset) => (
        <span className="tabular-nums">{utc(d.last_used_at) ?? d.last_used_at}</span>
      ),
    },
  ];
}

/** The totals line + the datasets table — datasets lead least-recently-used (the sweeper's order). */
function Inventory({ inventory }: { inventory: DataInventory }) {
  const { totals, datasets, sweep_interval_s } = inventory;
  // the clock ticks while the page is open so the sweep age never freezes at mount time
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(timer);
  }, []);
  const budget = totals.budget_bytes;
  const overBudget = budget > 0 && totals.bytes > budget;
  const sweepAgeMs = totals.last_sweep !== null ? now - Date.parse(totals.last_sweep) : null;
  const sweepStalled = sweepAgeMs !== null && sweepAgeMs > 2 * sweep_interval_s * 1000;
  return (
    <>
      <p className="mb-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-2">
        {overBudget && <StatusBadge status="OVER BUDGET" variant="error" size="sm" />}
        {sweepStalled && (
          <StatusBadge
            status="SWEEP STALLED"
            variant="warning"
            size="sm"
          />
        )}
        <span>
          {fmtBytes(totals.bytes)}
          {budget > 0
            ? ` of ${fmtBytes(budget)} (${Math.round((totals.bytes / budget) * 100)}%)`
            : ' cached — delete-after-run (cache_max_gb=0) evicts every eligible dataset each sweep'}
          {' · '}
          {totals.rows.toLocaleString('en-GB')} bars · {totals.pairs} {totals.pairs === 1 ? 'pair' : 'pairs'}
        </span>
      </p>
      <p className="mb-3 text-xs text-ink-2">
        Last sweep: {totals.last_sweep === null ? 'none yet' : `${utc(totals.last_sweep) ?? totals.last_sweep} (${age(sweepAgeMs ?? NaN) ?? 'age unknown'})`}
        {' '}· the sweeper runs every {Math.round(sweep_interval_s / 60)} min and evicts least-recently-used datasets over the budget
      </p>
      <Table
        columns={inventoryColumns()}
        data={datasets}
        keyExtractor={(d) => `${d.exchange}:${d.symbol}:${d.timeframe}`}
        emptyMessage="No auto-cached datasets yet. The first backtest on a cold pair fetches its bars; re-runs of resident data are instant."
      />
      <p className="mt-3 text-xs text-ink-2">
        Sizes are estimates: bars x the configured row-bytes estimate, calibrated on-box. A dataset&apos;s range
        is the coverage its fetch asked for — it can predate the first stored bar, and 0 bars means the exchange
        never served that range.
      </p>
    </>
  );
}
