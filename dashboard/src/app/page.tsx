'use client';

import { useEffect, useState, useCallback } from 'react';
import {
  Wallet,
  TrendingUp,
  BarChart3,
  ArrowDownRight,
} from 'lucide-react';
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts';
import Link from 'next/link';
import { StatCard } from '@/components/ui/DataCard';
import { DataCard } from '@/components/ui/DataCard';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { ErrorCard } from '@/components/ui/ErrorCard';
import { fetchApi } from '@/lib/api';
import { logger } from '@/lib/logger';

/* ---------- Types ---------- */

interface Trade {
  id: string;
  pair: string;
  side: 'Long' | 'Short';
  entry: string;
  exit: string;
  pnl: string;
  time: string;
}

interface PortfolioSummary {
  total_value: number;
  unrealized_pnl: number;
  realized_pnl: number;
  daily_pnl: number;
  max_drawdown_pct: number;
  total_fees: number;
  change_pct: number;
}

interface ApiRecentTrade {
  id: string;
  symbol: string;
  side: string;
  price: number;
  quantity: number;
  fee: number;
  pnl: number;
  timestamp: string;
}

interface ApiPosition {
  id: string;
  pair: string;
  side: string;
  size: number;
  entry_price: number;
  current_price: number;
  unrealized_pnl: number;
  pnl_pct: number;
}

interface EquityPoint {
  timestamp: string;
  value: number;
}

/* ---------- Helpers ---------- */

function formatSymbol(symbol: string): string {
  const match = symbol.match(/^([A-Z]{3,4})(USDT|USD|BUSD|USDC)$/);
  if (match) return `${match[1]}/${match[2]}`;
  return symbol;
}

function mapApiTrades(data: ApiRecentTrade[]): Trade[] {
  return data.map((t) => {
    const pnlStr = `${t.pnl >= 0 ? '+' : ''}$${Math.abs(t.pnl).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
    return {
      id: t.id,
      pair: formatSymbol(t.symbol),
      side: t.side === 'BUY' ? 'Long' as const : 'Short' as const,
      entry: `$${t.price.toLocaleString('en-US', { minimumFractionDigits: 2 })}`,
      exit: '-',
      pnl: pnlStr,
      time: new Date(t.timestamp).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' }),
    };
  });
}

const tradeColumns = [
  { key: 'pair', header: 'Pair' },
  {
    key: 'side',
    header: 'Side',
    render: (row: Trade) => (
      <StatusBadge
        status={row.side}
        variant={row.side === 'Long' ? 'success' : 'error'}
        size="sm"
      />
    ),
  },
  { key: 'entry', header: 'Entry' },
  { key: 'exit', header: 'Exit' },
  {
    key: 'pnl',
    header: 'PnL',
    render: (row: Trade) => (
      <span
        className={
          row.pnl.startsWith('+')
            ? 'text-status-success font-medium'
            : 'text-status-error font-medium'
        }
      >
        {row.pnl}
      </span>
    ),
  },
  { key: 'time', header: 'Time', hideOnMobile: true },
];

/* ---------- Recharts helpers ---------- */

function formatXTick(timestamp: string): string {
  const d = new Date(timestamp);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function formatYTick(value: number): string {
  return `$${(value / 1000).toFixed(0)}k`;
}

const CustomTooltip = ({ active, payload, label }: { active?: boolean; payload?: Array<{ value: number }>; label?: string }) => {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-border-default bg-bg-elevated px-3 py-2 shadow-lg">
      <p className="text-xs text-text-muted">{label ? formatXTick(label) : ''}</p>
      <p className="text-sm font-medium text-text-primary">
        ${payload[0].value.toLocaleString('en-US', { minimumFractionDigits: 2 })}
      </p>
    </div>
  );
};

/* ---------- Page ---------- */

export default function DashboardPage() {
  useEffect(() => { logger.info('Dashboard', 'Page mounted'); }, []);
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [recentTrades, setRecentTrades] = useState<Trade[] | null>(null);
  const [positionCount, setPositionCount] = useState<number | null>(null);
  // max_drawdown_pct now comes from portfolio summary
  const [equityCurve, setEquityCurve] = useState<EquityPoint[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [isDark, setIsDark] = useState(false);
  const [fetchErrors, setFetchErrors] = useState<Record<string, boolean>>({});

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains('dark'));
  }, []);

  const loadData = useCallback(async () => {
    setLoading(true);
    setFetchErrors({});

    const cfg = await fetchApi<{ trading_mode: string }>('/api/system/config')
      .catch(() => ({ trading_mode: 'paper' }));
    const source = cfg.trading_mode === 'live' ? 'live' : 'paper';
    const qs = `?source=${source}`;

    await Promise.all([
      fetchApi<PortfolioSummary>(`/api/portfolio/summary${qs}`)
        .then(setSummary)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, summary: true })); }),
      fetchApi<ApiRecentTrade[]>(`/api/portfolio/trades${qs}`)
        .then((data) => setRecentTrades(mapApiTrades(data)))
        .catch(() => { setFetchErrors((prev) => ({ ...prev, trades: true })); }),
      fetchApi<ApiPosition[]>(`/api/portfolio/positions${qs}`)
        .then((data) => setPositionCount(data.length))
        .catch(() => { setFetchErrors((prev) => ({ ...prev, positions: true })); }),
      // max drawdown now computed from portfolio summary
      fetchApi<EquityPoint[]>(`/api/portfolio/equity-curve${qs}`)
        .then((data) => setEquityCurve(data))
        .catch(() => { setFetchErrors((prev) => ({ ...prev, equity: true })); }),
    ]);

    setLoading(false);
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const accentColor = isDark ? '#2383e2' : '#2563eb';
  const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)';
  const textMuted = isDark ? 'rgba(255,255,255,0.44)' : '#475569';

  return (
    <div className="flex flex-col gap-6">
      {/* Stat cards */}
      {fetchErrors.summary ? (
        <ErrorCard message="Failed to load portfolio summary" onRetry={loadData} />
      ) : loading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-28 animate-pulse rounded-xl border border-border-default bg-bg-tertiary" />
          ))}
        </div>
      ) : summary ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatCard
            icon={Wallet}
            label="Portfolio Value"
            value={`$${summary.total_value.toLocaleString('en-US', { minimumFractionDigits: 2 })}`}
            change={summary.change_pct}
            changeType="increase"
          />
          <StatCard
            icon={TrendingUp}
            label="Daily PnL"
            value={`${summary.daily_pnl >= 0 ? '+' : ''}$${Math.abs(summary.daily_pnl).toLocaleString('en-US', { minimumFractionDigits: 2 })}`}
            change={summary.daily_pnl !== 0 ? Math.abs(summary.daily_pnl) : undefined}
            changeType={summary.daily_pnl >= 0 ? 'increase' : 'decrease'}
          />
          <Link href="/trading" className="block">
            <StatCard
              icon={BarChart3}
              label="Open Positions"
              value={positionCount != null ? positionCount.toLocaleString('en-US') : '-'}
              className="cursor-pointer hover:border-border-hover"
            />
          </Link>
          <StatCard
            icon={ArrowDownRight}
            label="Max Drawdown"
            value={`${summary.max_drawdown_pct.toLocaleString('en-US', { minimumFractionDigits: 1 })}%`}
            change={summary.max_drawdown_pct > 0 ? summary.max_drawdown_pct : undefined}
            changeType="decrease"
          />
        </div>
      ) : (
        <p className="text-sm text-text-muted text-center py-8">No data yet</p>
      )}

      {/* Equity curve */}
      <DataCard title="Equity Curve" description="Portfolio performance over time">
        {fetchErrors.equity ? (
          <ErrorCard message="Failed to load equity curve" onRetry={loadData} />
        ) : loading ? (
          <div className="flex h-64 items-center justify-center">
            <div className="h-64 w-full animate-pulse rounded-lg bg-bg-tertiary" />
          </div>
        ) : equityCurve && equityCurve.length > 0 ? (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={equityCurve} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={accentColor} stopOpacity={0.2} />
                    <stop offset="95%" stopColor={accentColor} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} vertical={false} />
                <XAxis
                  dataKey="timestamp"
                  tickFormatter={formatXTick}
                  tick={{ fontSize: 11, fill: textMuted }}
                  axisLine={false}
                  tickLine={false}
                  minTickGap={40}
                />
                <YAxis
                  tickFormatter={formatYTick}
                  tick={{ fontSize: 11, fill: textMuted }}
                  axisLine={false}
                  tickLine={false}
                  width={44}
                />
                <Tooltip content={<CustomTooltip />} />
                <Area
                  type="monotone"
                  dataKey="value"
                  stroke={accentColor}
                  strokeWidth={2}
                  fill="url(#equityGradient)"
                  dot={false}
                  activeDot={{ r: 4, fill: accentColor, strokeWidth: 0 }}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
            <div className="text-center">
              <div className="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-lg bg-bg-tertiary">
                <TrendingUp className="h-5 w-5 text-text-muted" />
              </div>
              <p className="text-sm font-medium text-text-muted">Equity Curve Chart</p>
              <p className="mt-1 text-xs text-text-light">No equity data available</p>
            </div>
          </div>
        )}
      </DataCard>

      {/* Recent trades */}
      <DataCard title="Recent Trades" description="Last 5 executed trades">
        {fetchErrors.trades ? (
          <ErrorCard message="Failed to load recent trades" onRetry={loadData} />
        ) : loading ? (
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-12 animate-pulse rounded-lg bg-bg-tertiary" />
            ))}
          </div>
        ) : recentTrades && recentTrades.length > 0 ? (
          <Table
            columns={tradeColumns}
            data={recentTrades}
            keyExtractor={(row) => row.id}
          />
        ) : (
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>
    </div>
  );
}
