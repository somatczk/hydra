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

/* ---------- Page ---------- */

export default function PortfolioPage() {
  return (
    <div className="flex flex-col gap-6">
      {/* Stat cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={Wallet}
          label="Total Value"
          value="$12,450.00"
          change={2.4}
          changeType="increase"
        />
        <StatCard
          icon={TrendingUp}
          label="Unrealized PnL"
          value="+$198.00"
          change={1.6}
          changeType="increase"
        />
        <StatCard
          icon={TrendingDown}
          label="Realized PnL"
          value="+$1,842.30"
          change={12.4}
          changeType="increase"
        />
        <StatCard
          icon={Receipt}
          label="Total Fees"
          value="$87.50"
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
                Recharts area chart
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
                Recharts bar chart
              </p>
            </div>
          </div>
        </DataCard>
      </div>

      {/* Allocation breakdown */}
      <DataCard title="Position Allocation" description="Current portfolio breakdown">
        <div className="space-y-3">
          {[
            { label: 'BTC/USDT Long', allocation: 45, value: '$5,602.50' },
            { label: 'BTC/USDT Short', allocation: 15, value: '$1,867.50' },
            { label: 'Cash (USDT)', allocation: 40, value: '$4,980.00' },
          ].map((item) => (
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
