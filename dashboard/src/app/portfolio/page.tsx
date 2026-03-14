'use client';

import { useEffect, useState } from 'react';
import {
  Wallet,
  TrendingUp,
  TrendingDown,
  Receipt,
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
import { StatCard } from '@/components/ui/DataCard';
import { DataCard } from '@/components/ui/DataCard';
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

/* ---------- Placeholder data ---------- */

const placeholderSummary: PortfolioSummary = {
  total_value: 12450.0,
  unrealized_pnl: 198.0,
  realized_pnl: 1842.3,
  total_fees: 87.5,
  change_pct: 2.4,
};

const placeholderPositions = [
  { label: 'BTC/USDT Long', allocation: 45, value: '$5,602.50' },
  { label: 'BTC/USDT Short', allocation: 15, value: '$1,867.50' },
  { label: 'Cash (USDT)', allocation: 40, value: '$4,980.00' },
];

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

/* ---------- Page ---------- */

export default function PortfolioPage() {
  useEffect(() => { logger.info('Portfolio', 'Page mounted'); }, []);
  const [summary, setSummary] = useState<PortfolioSummary>(placeholderSummary);
  const [positions, setPositions] = useState<Position[]>([]);
  const [equityCurve, setEquityCurve] = useState<EquityPoint[]>([]);
  const [dailyPnl, setDailyPnl] = useState<DailyPnl[]>([]);
  const [loading, setLoading] = useState(true);
  const [isDark, setIsDark] = useState(false);

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains('dark'));
  }, []);

  useEffect(() => {
    Promise.all([
      fetchApi<PortfolioSummary>('/api/portfolio/summary').catch(() => null),
      fetchApi<Position[]>('/api/portfolio/positions').catch(() => null),
      fetchApi<EquityPoint[]>('/api/portfolio/equity-curve').catch(() => null),
      fetchApi<DailyPnl[]>('/api/portfolio/daily-pnl').catch(() => null),
    ])
      .then(([s, p, ec, dp]) => {
        if (s) setSummary(s);
        if (p) setPositions(p);
        if (ec) setEquityCurve(ec);
        if (dp) setDailyPnl(dp);
      })
      .finally(() => setLoading(false));
  }, []);

  // Derive allocation from API positions or use placeholder
  const allocationItems = positions.length > 0
    ? positions.map((p) => {
        const posValue = p.size * p.current_price;
        const allocation = Math.round((posValue / summary.total_value) * 100);
        return {
          label: `${p.pair} ${p.side}`,
          allocation,
          value: `$${posValue.toLocaleString('en-US', { minimumFractionDigits: 2 })}`,
        };
      })
    : placeholderPositions;

  const drawdownData = computeDrawdown(equityCurve);

  const accentColor = isDark ? '#2383e2' : '#2563eb';
  const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)';
  const textMuted = isDark ? 'rgba(255,255,255,0.44)' : '#475569';

  const fmt = (v: number) => `$${v.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;

  return (
    <div className="flex flex-col gap-6">
      {/* Stat cards */}
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

      {/* Charts row */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Equity Curve */}
        <DataCard title="Equity Curve" description="Portfolio value over time">
          {equityCurve.length === 0 ? (
            <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
              <p className="text-sm text-text-muted">
                {loading ? 'Loading...' : 'No data'}
              </p>
            </div>
          ) : (
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
          )}
        </DataCard>

        {/* Drawdown Chart */}
        <DataCard title="Drawdown" description="Maximum drawdown over time">
          {equityCurve.length === 0 ? (
            <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
              <p className="text-sm text-text-muted">
                {loading ? 'Loading...' : 'No data'}
              </p>
            </div>
          ) : (
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
          )}
        </DataCard>
      </div>

      {/* Allocation breakdown */}
      <DataCard title="Position Allocation" description="Current portfolio breakdown">
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
      </DataCard>
    </div>
  );
}
