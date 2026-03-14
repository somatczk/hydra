'use client';

import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowUpDown, Eye, OctagonX } from 'lucide-react';
import { DataCard } from '@/components/ui/DataCard';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { fetchApi } from '@/lib/api';
import { logger } from '@/lib/logger';

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

interface TradingSession {
  session_id: string;
  strategy_id: string;
  trading_mode: string;
  status: string;
  symbols: string[];
  timeframe: string;
  started_at: string | null;
}

interface RiskConfig {
  kill_switch_active: boolean;
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

/* ---------- Page ---------- */

export default function TradingPage() {
  const router = useRouter();
  useEffect(() => { logger.info('Trading', 'Page mounted'); }, []);
  const [openPositions, setOpenPositions] = useState<Position[]>(placeholderPositions);
  const [recentTrades, setRecentTrades] = useState<RecentTrade[]>(placeholderTrades);
  const [sessions, setSessions] = useState<TradingSession[]>([]);
  const [killSwitchActive, setKillSwitchActive] = useState(false);
  const [loading, setLoading] = useState(true);

  const widgetContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    Promise.all([
      fetchApi<ApiPosition[]>('/api/portfolio/positions')
        .then((data) => setOpenPositions(mapApiPositions(data)))
        .catch(() => { /* keep placeholder */ }),
      fetchApi<ApiRecentTrade[]>('/api/portfolio/trades')
        .then((data) => setRecentTrades(mapApiTrades(data)))
        .catch(() => { /* keep placeholder */ }),
      fetchApi<TradingSession[]>('/api/trading/sessions')
        .then((data) => setSessions(data.filter((s) => s.status === 'running')))
        .catch(() => { /* keep empty */ }),
      fetchApi<RiskConfig>('/api/risk/config')
        .then((cfg) => setKillSwitchActive(cfg.kill_switch_active))
        .catch(() => { /* keep false */ }),
    ]).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    const container = widgetContainerRef.current;
    if (!container) return;

    const isDark = document.documentElement.classList.contains('dark');

    const script = document.createElement('script');
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
    script.type = 'text/javascript';
    script.async = true;
    script.innerHTML = JSON.stringify({
      autosize: true,
      symbol: 'BINANCE:BTCUSDT',
      interval: '60',
      timezone: 'Etc/UTC',
      theme: isDark ? 'dark' : 'light',
      style: '1',
      locale: 'en',
      allow_symbol_change: true,
      support_host: 'https://www.tradingview.com',
    });

    container.appendChild(script);

    return () => {
      container.innerHTML = '';
    };
  }, []);

  const runningSessions = sessions.filter((s) => s.status === 'running');

  return (
    <div className="flex flex-col gap-6">
      {/* Kill switch banner */}
      {killSwitchActive && (
        <div className="rounded-xl border-2 border-status-error bg-status-error/10 p-4 flex items-center gap-3">
          <OctagonX className="h-6 w-6 text-status-error shrink-0" />
          <div>
            <p className="text-sm font-semibold text-status-error">Kill Switch Active</p>
            <p className="text-xs text-text-muted">
              All trading is halted. Release the kill switch from the Risk page to resume.
            </p>
          </div>
        </div>
      )}

      {/* Active sessions */}
      {runningSessions.length > 0 && (
        <DataCard title="Running Sessions" description="Currently active trading sessions">
          <div className="space-y-2">
            {runningSessions.map((session) => (
              <div
                key={session.session_id}
                className="flex items-center justify-between rounded-lg border border-border-default bg-bg-secondary p-3 cursor-pointer hover:border-border-hover transition-colors"
                onClick={() => router.push(`/trading/${session.session_id}`)}
              >
                <div className="flex items-center gap-3">
                  <StatusBadge
                    status={session.trading_mode === 'paper' ? 'Paper' : 'Live'}
                    variant={session.trading_mode === 'paper' ? 'info' : 'warning'}
                    size="sm"
                  />
                  <div>
                    <p className="text-sm font-medium text-text-primary">{session.strategy_id}</p>
                    <p className="text-xs text-text-muted">
                      {session.symbols.join(', ')} &middot; {session.timeframe}
                      {session.started_at && ` &middot; Started ${new Date(session.started_at).toLocaleTimeString()}`}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <StatusBadge status="Running" variant="success" size="sm" />
                  <Eye className="h-4 w-4 text-text-muted" />
                </div>
              </div>
            ))}
          </div>
        </DataCard>
      )}

      {/* Candlestick chart */}
      <DataCard title="BTC/USDT" description="Live price chart">
        <div className="tradingview-widget-container h-[28rem] md:h-[36rem] w-full" ref={widgetContainerRef}>
          <div className="tradingview-widget-container__widget h-full w-full" />
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
          data={recentTrades}
          keyExtractor={(row) => row.id}
        />
      </DataCard>
    </div>
  );
}
