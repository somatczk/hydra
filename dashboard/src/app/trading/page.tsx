'use client';

import { useEffect, useState } from 'react';
import { CandlestickChart, ArrowUpDown } from 'lucide-react';
import { DataCard } from '@/components/ui/DataCard';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { fetchApi } from '@/lib/api';

/* ---------- Types ---------- */

interface Position {
  id: string;
  pair: string;
  side: 'Long' | 'Short';
  size: string;
  entry: string;
  current: string;
  pnl: string;
  pnlPercent: string;
}

interface RecentTrade {
  id: string;
  pair: string;
  side: 'Long' | 'Short';
  size: string;
  price: string;
  time: string;
  status: string;
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

/* ---------- Placeholder data ---------- */

const placeholderPositions: Position[] = [
  { id: '1', pair: 'BTC/USDT', side: 'Long', size: '0.15 BTC', entry: '$67,420', current: '$68,100', pnl: '+$102.00', pnlPercent: '+1.01%' },
  { id: '2', pair: 'BTC/USDT', side: 'Short', size: '0.08 BTC', entry: '$68,800', current: '$68,100', pnl: '+$56.00', pnlPercent: '+1.02%' },
  { id: '3', pair: 'BTC/USDT', side: 'Long', size: '0.20 BTC', entry: '$67,900', current: '$68,100', pnl: '+$40.00', pnlPercent: '+0.29%' },
];

const placeholderTrades: RecentTrade[] = [
  { id: '1', pair: 'BTC/USDT', side: 'Long', size: '0.10 BTC', price: '$67,420', time: '14:32:15', status: 'Filled' },
  { id: '2', pair: 'BTC/USDT', side: 'Short', size: '0.05 BTC', price: '$68,350', time: '13:15:42', status: 'Filled' },
  { id: '3', pair: 'BTC/USDT', side: 'Long', size: '0.12 BTC', price: '$67,100', time: '11:48:09', status: 'Filled' },
  { id: '4', pair: 'BTC/USDT', side: 'Long', size: '0.08 BTC', price: '$66,800', time: '10:22:33', status: 'Cancelled' },
];

function mapApiPositions(data: ApiPosition[]): Position[] {
  const fmt = (v: number) => `$${v.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
  return data.map((p) => ({
    id: p.id,
    pair: p.pair,
    side: p.side as 'Long' | 'Short',
    size: `${p.size} BTC`,
    entry: fmt(p.entry_price),
    current: fmt(p.current_price),
    pnl: `${p.unrealized_pnl >= 0 ? '+' : ''}${fmt(p.unrealized_pnl)}`,
    pnlPercent: `${p.pnl_pct >= 0 ? '+' : ''}${p.pnl_pct.toFixed(2)}%`,
  }));
}

const positionColumns = [
  { key: 'pair', header: 'Pair' },
  {
    key: 'side',
    header: 'Side',
    render: (row: Position) => (
      <StatusBadge
        status={row.side}
        variant={row.side === 'Long' ? 'success' : 'error'}
        size="sm"
      />
    ),
  },
  { key: 'size', header: 'Size' },
  { key: 'entry', header: 'Entry', hideOnMobile: true },
  { key: 'current', header: 'Current', hideOnMobile: true },
  {
    key: 'pnl',
    header: 'PnL',
    render: (row: Position) => (
      <div>
        <span
          className={
            row.pnl.startsWith('+')
              ? 'text-status-success font-medium'
              : 'text-status-error font-medium'
          }
        >
          {row.pnl}
        </span>
        <span className="ml-1 text-xs text-text-muted">{row.pnlPercent}</span>
      </div>
    ),
  },
];

const tradeHistoryColumns = [
  { key: 'pair', header: 'Pair' },
  {
    key: 'side',
    header: 'Side',
    render: (row: RecentTrade) => (
      <StatusBadge
        status={row.side}
        variant={row.side === 'Long' ? 'success' : 'error'}
        size="sm"
      />
    ),
  },
  { key: 'size', header: 'Size' },
  { key: 'price', header: 'Price' },
  { key: 'time', header: 'Time', hideOnMobile: true },
  {
    key: 'status',
    header: 'Status',
    render: (row: RecentTrade) => (
      <StatusBadge
        status={row.status}
        variant={row.status === 'Filled' ? 'success' : 'neutral'}
        size="sm"
      />
    ),
  },
];

/* ---------- Page ---------- */

export default function TradingPage() {
  const [openPositions, setOpenPositions] = useState<Position[]>(placeholderPositions);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchApi<ApiPosition[]>('/api/portfolio/positions')
      .then((data) => setOpenPositions(mapApiPositions(data)))
      .catch(() => { /* keep placeholder */ })
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="flex flex-col gap-6">
      {/* Chart placeholder */}
      <DataCard title="BTC/USDT" description="Live price chart">
        <div className="flex h-80 md:h-[28rem] items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
          <div className="text-center">
            <div className="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-lg bg-bg-tertiary">
              <CandlestickChart className="h-5 w-5 text-text-muted" />
            </div>
            <p className="text-sm font-medium text-text-muted">
              BTC/USDT TradingView Chart
            </p>
            <p className="mt-1 text-xs text-text-light">
              {loading ? 'Loading...' : 'Lightweight Charts will render here'}
            </p>
          </div>
        </div>
      </DataCard>

      {/* Open positions */}
      <DataCard title="Open Positions" description="Currently active positions">
        <Table
          columns={positionColumns}
          data={openPositions}
          keyExtractor={(row) => row.id}
        />
      </DataCard>

      {/* Recent trades */}
      <DataCard title="Recent Trades">
        <div className="flex items-center gap-2 mb-3">
          <ArrowUpDown className="h-4 w-4 text-text-muted" />
          <span className="text-xs text-text-muted">Sorted by most recent</span>
        </div>
        <Table
          columns={tradeHistoryColumns}
          data={placeholderTrades}
          keyExtractor={(row) => row.id}
        />
      </DataCard>
    </div>
  );
}
