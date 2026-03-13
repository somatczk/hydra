import {
  Wallet,
  TrendingUp,
  BarChart3,
  ArrowDownRight,
} from 'lucide-react';
import { StatCard } from '@/components/ui/DataCard';
import { DataCard } from '@/components/ui/DataCard';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';

/* ---------- Mock data ---------- */

interface Trade {
  id: string;
  pair: string;
  side: 'Long' | 'Short';
  entry: string;
  exit: string;
  pnl: string;
  time: string;
}

const recentTrades: Trade[] = [
  { id: '1', pair: 'BTC/USDT', side: 'Long', entry: '$67,420', exit: '$68,180', pnl: '+$152.40', time: '14:32' },
  { id: '2', pair: 'BTC/USDT', side: 'Short', entry: '$68,350', exit: '$67,890', pnl: '+$92.00', time: '13:15' },
  { id: '3', pair: 'BTC/USDT', side: 'Long', entry: '$67,100', exit: '$67,050', pnl: '-$10.00', time: '11:48' },
  { id: '4', pair: 'BTC/USDT', side: 'Long', entry: '$66,800', exit: '$67,300', pnl: '+$100.00', time: '10:22' },
  { id: '5', pair: 'BTC/USDT', side: 'Short', entry: '$67,500', exit: '$67,680', pnl: '-$48.90', time: '09:05' },
];

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

/* ---------- Page ---------- */

export default function DashboardPage() {
  return (
    <div className="flex flex-col gap-6">
      {/* Stat cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={Wallet}
          label="Portfolio Value"
          value="$12,450.00"
          change={2.4}
          changeType="increase"
        />
        <StatCard
          icon={TrendingUp}
          label="Daily PnL"
          value="+$285.50"
          change={1.8}
          changeType="increase"
        />
        <StatCard
          icon={BarChart3}
          label="Open Positions"
          value="3"
        />
        <StatCard
          icon={ArrowDownRight}
          label="Max Drawdown"
          value="4.2%"
          change={0.3}
          changeType="decrease"
        />
      </div>

      {/* Equity curve placeholder */}
      <DataCard title="Equity Curve" description="Portfolio performance over time">
        <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
          <div className="text-center">
            <div className="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-lg bg-bg-tertiary">
              <TrendingUp className="h-5 w-5 text-text-muted" />
            </div>
            <p className="text-sm font-medium text-text-muted">Equity Curve Chart</p>
            <p className="mt-1 text-xs text-text-light">
              Recharts area chart will render here
            </p>
          </div>
        </div>
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
