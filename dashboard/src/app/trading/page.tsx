'use client';

import { useEffect, useRef, useState } from 'react';
import { CandlestickChart, ArrowUpDown } from 'lucide-react';
import { createChart, ColorType, type IChartApi, type UTCTimestamp } from 'lightweight-charts';
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

function formatSymbol(symbol: string): string {
  // "BTCUSDT" -> "BTC/USDT"
  const match = symbol.match(/^([A-Z]{3,4})(USDT|USD|BUSD|USDC)$/);
  if (match) return `${match[1]}/${match[2]}`;
  return symbol;
}

function mapApiTrades(data: ApiRecentTrade[]): RecentTrade[] {
  return data.map((t) => ({
    id: t.id,
    pair: formatSymbol(t.symbol),
    side: t.side === 'BUY' ? 'Long' as const : 'Short' as const,
    size: `${t.quantity.toLocaleString('en-US')} BTC`,
    price: `$${t.price.toLocaleString('en-US', { minimumFractionDigits: 2 })}`,
    time: new Date(t.timestamp).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }),
    status: 'Filled',
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

/* ---------- Sample candle data ---------- */

interface CandleBar {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
}

function generateCandleData(count: number): CandleBar[] {
  const data: CandleBar[] = [];
  let price = 68000;
  const now = Math.floor(Date.now() / 1000);
  for (let i = count; i > 0; i--) {
    const time = (now - i * 3600) as UTCTimestamp; // hourly candles
    const change = (Math.random() - 0.48) * 500;
    const open = price;
    price = Math.max(price + change, 50000);
    const close = price;
    const high = Math.max(open, close) + Math.random() * 200;
    const low = Math.min(open, close) - Math.random() * 200;
    data.push({ time, open, high, low, close });
  }
  return data;
}

/* ---------- Page ---------- */

export default function TradingPage() {
  const [openPositions, setOpenPositions] = useState<Position[]>(placeholderPositions);
  const [recentTrades, setRecentTrades] = useState<RecentTrade[]>(placeholderTrades);
  const [loading, setLoading] = useState(true);

  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    Promise.all([
      fetchApi<ApiPosition[]>('/api/portfolio/positions')
        .then((data) => setOpenPositions(mapApiPositions(data)))
        .catch(() => { /* keep placeholder */ }),
      fetchApi<ApiRecentTrade[]>('/api/portfolio/trades')
        .then((data) => setRecentTrades(mapApiTrades(data)))
        .catch(() => { /* keep placeholder */ }),
    ]).finally(() => setLoading(false));
  }, []);

  // Create the lightweight-charts candlestick chart
  useEffect(() => {
    if (!containerRef.current) return;

    const isDark = document.documentElement.classList.contains('dark');

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: isDark ? 'rgba(255,255,255,0.44)' : '#475569',
      },
      grid: {
        vertLines: { color: isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)' },
        horzLines: { color: isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)' },
      },
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight,
      timeScale: { timeVisible: true, secondsVisible: false },
    });

    chartRef.current = chart;

    const series = chart.addCandlestickSeries({
      upColor: isDark ? '#35a569' : '#22c55e',
      downColor: isDark ? '#eb5757' : '#ef4444',
      borderVisible: false,
      wickUpColor: isDark ? '#35a569' : '#22c55e',
      wickDownColor: isDark ? '#eb5757' : '#ef4444',
    });

    series.setData(generateCandleData(100));
    chart.timeScale().fitContent();

    // Resize observer keeps chart dimensions in sync with container
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry && chartRef.current) {
        chartRef.current.applyOptions({
          width: entry.contentRect.width,
          height: entry.contentRect.height,
        });
      }
    });

    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  return (
    <div className="flex flex-col gap-6">
      {/* Candlestick chart */}
      <DataCard title="BTC/USDT" description="Live price chart">
        <div ref={containerRef} className="h-80 md:h-[28rem] w-full" />
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
          data={recentTrades}
          keyExtractor={(row) => row.id}
        />
      </DataCard>
    </div>
  );
}
