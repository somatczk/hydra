'use client';

import { useEffect, useState, useCallback } from 'react';
import {
  Wallet,
  TrendingUp,
  TrendingDown,
  Receipt,
} from 'lucide-react';
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Cell,
} from 'recharts';
import { StatCard } from '@/components/ui/DataCard';
import { DataCard } from '@/components/ui/DataCard';
import { ErrorCard } from '@/components/ui/ErrorCard';
import { fetchApi } from '@/lib/api';
import { logger } from '@/lib/logger';

/* ---------- Types ---------- */

interface PortfolioSummary {
  total_value: number;
  unrealized_pnl: number;
  realized_pnl: number;
  total_fees: number;
  change_pct: number;
}

interface Position {
  id: string;
  pair: string;
  exchange: string;
  side: string;
  size: number;
  entry_price: number;
  current_price: number;
  unrealized_pnl: number;
  pnl_pct: number;
}

interface EquityPoint { timestamp: string; value: number; }
interface DailyPnl { date: string; pnl: number; }

interface MonthlyReturn {
  year: number;
  month: number;
  return_pct: number;
}

interface AttributionItem {
  strategy: string;
  pnl: number;
}

/* ---------- Utilities ---------- */

function computeDrawdown(curve: EquityPoint[]): Array<{ timestamp: string; drawdown: number }> {
  let peak = 0;
  return curve.map((p) => {
    if (p.value > peak) peak = p.value;
    const dd = peak > 0 ? ((peak - p.value) / peak) * 100 : 0;
    return { timestamp: p.timestamp, drawdown: dd };
  });
}

function formatXTick(timestamp: string): string {
  const d = new Date(timestamp);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function formatYTick(value: number): string {
  return `$${(value / 1000).toFixed(0)}k`;
}

function formatDrawdownTick(value: number): string {
  return `${value.toFixed(1)}%`;
}

const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function getReturnColor(pct: number): string {
  if (pct > 5) return 'bg-status-success';
  if (pct > 2) return 'bg-status-success/70';
  if (pct > 0) return 'bg-status-success/40';
  if (pct === 0) return 'bg-bg-tertiary';
  if (pct > -2) return 'bg-status-error/40';
  if (pct > -5) return 'bg-status-error/70';
  return 'bg-status-error';
}

/* ---------- Page ---------- */

export default function PortfolioPage() {
  useEffect(() => { logger.info('Portfolio', 'Page mounted'); }, []);
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [equityCurve, setEquityCurve] = useState<EquityPoint[] | null>(null);
  const [dailyPnl, setDailyPnl] = useState<DailyPnl[]>([]);
  const [monthlyReturns, setMonthlyReturns] = useState<MonthlyReturn[] | null>(null);
  const [attribution, setAttribution] = useState<AttributionItem[] | null>(null);
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
      fetchApi<Position[]>(`/api/portfolio/positions${qs}`)
        .then(setPositions)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, positions: true })); }),
      fetchApi<EquityPoint[]>(`/api/portfolio/equity-curve${qs}`)
        .then(setEquityCurve)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, equity: true })); }),
      fetchApi<DailyPnl[]>(`/api/portfolio/daily-pnl${qs}`)
        .then(setDailyPnl)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, dailyPnl: true })); }),
      fetchApi<MonthlyReturn[]>(`/api/portfolio/monthly-returns${qs}`)
        .then(setMonthlyReturns)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, monthlyReturns: true })); }),
      fetchApi<AttributionItem[]>(`/api/portfolio/attribution${qs}`)
        .then(setAttribution)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, attribution: true })); }),
    ]);

    setLoading(false);
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  // Derive allocation from API positions
  const allocationItems = positions.length > 0 && summary
    ? positions.map((p) => {
        const posValue = p.size * p.current_price;
        const allocation = Math.round((posValue / summary.total_value) * 100);
        return {
          label: `${p.pair} ${p.side}`,
          allocation,
          value: `$${posValue.toLocaleString('en-US', { minimumFractionDigits: 2 })}`,
        };
      })
    : [];

  const drawdownData = equityCurve ? computeDrawdown(equityCurve) : [];

  const accentColor = isDark ? '#2383e2' : '#2563eb';
  const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)';
  const textMuted = isDark ? 'rgba(255,255,255,0.44)' : '#475569';

  const fmt = (v: number) => `$${v.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;

  // Group monthly returns by year
  const monthlyByYear: Record<number, Record<number, number>> = {};
  if (monthlyReturns) {
    for (const mr of monthlyReturns) {
      if (!monthlyByYear[mr.year]) monthlyByYear[mr.year] = {};
      monthlyByYear[mr.year][mr.month] = mr.return_pct;
    }
  }
  const years = Object.keys(monthlyByYear).map(Number).sort();

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
            label="Total Value"
            value={fmt(summary.total_value)}
            change={summary.change_pct}
            changeType="increase"
          />
          <StatCard
            icon={TrendingUp}
            label="Unrealized PnL"
            value={`+${fmt(summary.unrealized_pnl)}`}
            change={1.6}
            changeType="increase"
          />
          <StatCard
            icon={TrendingDown}
            label="Realized PnL"
            value={`+${fmt(summary.realized_pnl)}`}
            change={12.4}
            changeType="increase"
          />
          <StatCard
            icon={Receipt}
            label="Total Fees"
            value={fmt(summary.total_fees)}
            change={0.7}
            changeType="decrease"
          />
        </div>
      ) : (
        <p className="text-sm text-text-muted text-center py-8">No data yet</p>
      )}

      {/* Charts row */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Equity Curve */}
        <DataCard title="Equity Curve" description="Portfolio value over time">
          {fetchErrors.equity ? (
            <ErrorCard message="Failed to load equity curve" onRetry={loadData} />
          ) : loading ? (
            <div className="h-64 animate-pulse rounded-lg bg-bg-tertiary" />
          ) : equityCurve && equityCurve.length > 0 ? (
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={equityCurve} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="portfolioEquityGradient" x1="0" y1="0" x2="0" y2="1">
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
                  <Tooltip
                    contentStyle={{
                      background: isDark ? '#1e2433' : '#ffffff',
                      border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : '#e2e8f0'}`,
                      borderRadius: '8px',
                      fontSize: '12px',
                    }}
                    formatter={(v: number) => [`$${v.toLocaleString('en-US', { minimumFractionDigits: 2 })}`, 'Value']}
                    labelFormatter={formatXTick}
                  />
                  <Area
                    type="monotone"
                    dataKey="value"
                    stroke={accentColor}
                    strokeWidth={2}
                    fill="url(#portfolioEquityGradient)"
                    dot={false}
                    activeDot={{ r: 4, fill: accentColor, strokeWidth: 0 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
              <p className="text-sm text-text-muted">No data yet</p>
            </div>
          )}
        </DataCard>

        {/* Drawdown Chart */}
        <DataCard title="Drawdown" description="Maximum drawdown over time">
          {fetchErrors.equity ? (
            <ErrorCard message="Failed to load drawdown data" onRetry={loadData} />
          ) : loading ? (
            <div className="h-64 animate-pulse rounded-lg bg-bg-tertiary" />
          ) : equityCurve && equityCurve.length > 0 ? (
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={drawdownData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="portfolioDrawdownGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#ef4444" stopOpacity={0.2} />
                      <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
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
                    tickFormatter={formatDrawdownTick}
                    tick={{ fontSize: 11, fill: textMuted }}
                    axisLine={false}
                    tickLine={false}
                    width={44}
                    reversed
                  />
                  <Tooltip
                    contentStyle={{
                      background: isDark ? '#1e2433' : '#ffffff',
                      border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : '#e2e8f0'}`,
                      borderRadius: '8px',
                      fontSize: '12px',
                    }}
                    formatter={(v: number) => [`${v.toFixed(2)}%`, 'Drawdown']}
                    labelFormatter={formatXTick}
                  />
                  <Area
                    type="monotone"
                    dataKey="drawdown"
                    stroke="#ef4444"
                    strokeWidth={2}
                    fill="url(#portfolioDrawdownGradient)"
                    dot={false}
                    activeDot={{ r: 4, fill: '#ef4444', strokeWidth: 0 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
              <p className="text-sm text-text-muted">No data yet</p>
            </div>
          )}
        </DataCard>
      </div>

      {/* Monthly returns heatmap */}
      <DataCard title="Monthly Returns" description="Calendar heatmap of monthly performance">
        {fetchErrors.monthlyReturns ? (
          <ErrorCard message="Failed to load monthly returns" onRetry={loadData} />
        ) : loading ? (
          <div className="h-32 animate-pulse rounded-lg bg-bg-tertiary" />
        ) : monthlyReturns && monthlyReturns.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr>
                  <th className="px-2 py-1 text-left text-text-muted font-semibold">Year</th>
                  {MONTH_LABELS.map((m) => (
                    <th key={m} className="px-1 py-1 text-center text-text-muted font-medium">{m}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {years.map((year) => (
                  <tr key={year}>
                    <td className="px-2 py-1 text-text-primary font-medium">{year}</td>
                    {Array.from({ length: 12 }).map((_, mi) => {
                      const val = monthlyByYear[year]?.[mi + 1];
                      return (
                        <td key={mi} className="px-1 py-1">
                          {val !== undefined ? (
                            <div
                              className={`rounded px-1.5 py-1 text-center text-xs font-medium text-white ${getReturnColor(val)}`}
                              title={`${MONTH_LABELS[mi]} ${year}: ${val >= 0 ? '+' : ''}${val.toFixed(1)}%`}
                            >
                              {val >= 0 ? '+' : ''}{val.toFixed(1)}%
                            </div>
                          ) : (
                            <div className="rounded px-1.5 py-1 text-center text-xs text-text-light bg-bg-secondary">
                              -
                            </div>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>

      {/* Attribution bar chart */}
      <DataCard title="Strategy Attribution" description="PnL contribution per strategy">
        {fetchErrors.attribution ? (
          <ErrorCard message="Failed to load attribution data" onRetry={loadData} />
        ) : loading ? (
          <div className="h-56 animate-pulse rounded-lg bg-bg-tertiary" />
        ) : attribution && attribution.length > 0 ? (
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={attribution}
                layout="vertical"
                margin={{ top: 4, right: 4, left: 0, bottom: 0 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} horizontal={false} />
                <XAxis
                  type="number"
                  tickFormatter={(v: number) => `$${v}`}
                  tick={{ fontSize: 11, fill: textMuted }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  type="category"
                  dataKey="strategy"
                  tick={{ fontSize: 11, fill: textMuted }}
                  axisLine={false}
                  tickLine={false}
                  width={120}
                />
                <Tooltip
                  contentStyle={{
                    background: isDark ? '#1e2433' : '#ffffff',
                    border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : '#e2e8f0'}`,
                    borderRadius: '8px',
                    fontSize: '12px',
                  }}
                  formatter={(v: number) => [`$${v.toFixed(2)}`, 'PnL']}
                />
                <Bar dataKey="pnl" radius={[0, 3, 3, 0]}>
                  {attribution.map((entry, index) => (
                    <Cell
                      key={`attr-${index}`}
                      fill={entry.pnl >= 0 ? '#22c55e' : '#ef4444'}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>

      {/* Allocation breakdown */}
      <DataCard title="Position Allocation" description="Current portfolio breakdown">
        {fetchErrors.positions ? (
          <ErrorCard message="Failed to load positions" onRetry={loadData} />
        ) : loading ? (
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-8 animate-pulse rounded-lg bg-bg-tertiary" />
            ))}
          </div>
        ) : allocationItems.length > 0 ? (
          <div className="space-y-3">
            {allocationItems.map((item) => (
              <div key={item.label}>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm text-text-primary">{item.label}</span>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-text-primary">{item.value}</span>
                    <span className="text-xs text-text-muted">{item.allocation}%</span>
                  </div>
                </div>
                <div className="h-2 rounded-full bg-bg-tertiary overflow-hidden">
                  <div
                    className="h-full rounded-full bg-accent-primary transition-all"
                    style={{ width: `${item.allocation}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>
    </div>
  );
}
