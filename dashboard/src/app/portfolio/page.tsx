'use client';

import { useEffect, useState } from 'react';
import {
  Wallet,
  TrendingUp,
  TrendingDown,
  Receipt,
  LineChart,
  ArrowDownRight,
} from 'lucide-react';
import { StatCard } from '@/components/ui/DataCard';
import { DataCard } from '@/components/ui/DataCard';
import { fetchApi } from '@/lib/api';

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

/* ---------- Page ---------- */

export default function PortfolioPage() {
  const [summary, setSummary] = useState<PortfolioSummary>(placeholderSummary);
  const [positions, setPositions] = useState<Position[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetchApi<PortfolioSummary>('/api/portfolio/summary').catch(() => null),
      fetchApi<Position[]>('/api/portfolio/positions').catch(() => null),
    ])
      .then(([s, p]) => {
        if (s) setSummary(s);
        if (p) setPositions(p);
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
          <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
            <div className="text-center">
              <div className="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-lg bg-bg-tertiary">
                <LineChart className="h-5 w-5 text-text-muted" />
              </div>
              <p className="text-sm font-medium text-text-muted">Equity Curve</p>
              <p className="mt-1 text-xs text-text-light">
                {loading ? 'Loading...' : 'Recharts area chart'}
              </p>
            </div>
          </div>
        </DataCard>

        {/* Drawdown Chart */}
        <DataCard title="Drawdown" description="Maximum drawdown over time">
          <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
            <div className="text-center">
              <div className="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-lg bg-bg-tertiary">
                <ArrowDownRight className="h-5 w-5 text-text-muted" />
              </div>
              <p className="text-sm font-medium text-text-muted">Drawdown Chart</p>
              <p className="mt-1 text-xs text-text-light">
                {loading ? 'Loading...' : 'Recharts bar chart'}
              </p>
            </div>
          </div>
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
