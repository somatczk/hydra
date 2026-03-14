'use client';

import { useEffect, useState } from 'react';
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

interface ApiRiskStatus {
  current_drawdown: number;
  max_drawdown_limit: number;
  daily_loss: number;
  daily_loss_limit: number;
  circuit_breakers: Array<{
    tier: number;
    label: string;
    threshold: string;
    current_value: number;
    status: string;
  }>;
}

interface EquityPoint {
  timestamp: string;
  value: number;
}

/* ---------- Placeholder data ---------- */

const placeholderTrades: Trade[] = [
  { id: '1', pair: 'BTC/USDT', side: 'Long', entry: '$67,420', exit: '$68,180', pnl: '+$152.40', time: '14:32' },
  { id: '2', pair: 'BTC/USDT', side: 'Short', entry: '$68,350', exit: '$67,890', pnl: '+$92.00', time: '13:15' },
  { id: '3', pair: 'BTC/USDT', side: 'Long', entry: '$67,100', exit: '$67,050', pnl: '-$10.00', time: '11:48' },
  { id: '4', pair: 'BTC/USDT', side: 'Long', entry: '$66,800', exit: '$67,300', pnl: '+$100.00', time: '10:22' },
  { id: '5', pair: 'BTC/USDT', side: 'Short', entry: '$67,500', exit: '$67,680', pnl: '-$48.90', time: '09:05' },
];

const placeholderSummary: PortfolioSummary = {
  total_value: 12450.0,
  unrealized_pnl: 198.0,
  realized_pnl: 1842.3,
  total_fees: 87.5,
  change_pct: 2.4,
};

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
  const [summary, setSummary] = useState<PortfolioSummary>(placeholderSummary);
  const [recentTrades, setRecentTrades] = useState<Trade[]>(placeholderTrades);
  const [positionCount, setPositionCount] = useState<number>(3);
  const [maxDrawdown, setMaxDrawdown] = useState<number>(4.2);
  const [equityCurve, setEquityCurve] = useState<EquityPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [isDark, setIsDark] = useState(false);

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains('dark'));
  }, []);

  useEffect(() => {
    Promise.all([
      fetchApi<PortfolioSummary>('/api/portfolio/summary')
        .then(setSummary)
        .catch(() => { /* keep placeholder */ }),
      fetchApi<ApiRecentTrade[]>('/api/portfolio/trades')
        .then((data) => setRecentTrades(mapApiTrades(data)))
        .catch(() => { /* keep placeholder */ }),
      fetchApi<ApiPosition[]>('/api/portfolio/positions')
        .then((data) => setPositionCount(data.length))
        .catch(() => { /* keep placeholder */ }),
      fetchApi<ApiRiskStatus>('/api/risk/status')
        .then((data) => setMaxDrawdown(data.current_drawdown))
        .catch(() => { /* keep placeholder */ }),
      fetchApi<EquityPoint[]>('/api/portfolio/equity-curve')
        .then((data) => setEquityCurve(data))
        .catch(() => { /* chart stays empty if API unavailable */ }),
    ]).finally(() => setLoading(false));
  }, []);

  const accentColor = isDark ? '#2383e2' : '#2563eb';
  const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)';
  const textMuted = isDark ? 'rgba(255,255,255,0.44)' : '#475569';

  return (
    <div className="flex flex-col gap-6">
      {/* Stat cards */}
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
          value={`+$${summary.unrealized_pnl.toFixed(2)}`}
          change={1.8}
          changeType="increase"
        />
        <Link href="/trading" className="block">
          <StatCard
            icon={BarChart3}
            label="Open Positions"
            value={positionCount.toLocaleString('en-US')}
            className="cursor-pointer hover:border-border-hover"
          />
        </Link>
        <StatCard
          icon={ArrowDownRight}
          label="Max Drawdown"
          value={`${maxDrawdown.toLocaleString('en-US', { minimumFractionDigits: 1 })}%`}
          change={0.3}
          changeType="decrease"
        />
      </div>

      {/* Equity curve */}
      <DataCard title="Equity Curve" description="Portfolio performance over time">
        {loading ? (
          <div className="flex h-64 items-center justify-center">
            <p className="text-sm text-text-muted">Loading...</p>
          </div>
        ) : equityCurve.length > 0 ? (
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
        <Table
          columns={tradeColumns}
          data={recentTrades}
          keyExtractor={(row) => row.id}
        />
      </DataCard>
    </div>
  );
}
