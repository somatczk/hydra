'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { ArrowLeft, Square, AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { DataCard } from '@/components/ui/DataCard';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { logger } from '@/lib/logger';

/* ---------- Types ---------- */

interface PositionItem {
  symbol: string;
  direction: string;
  quantity: number;
  avg_entry_price: number;
  unrealized_pnl: number;
}

interface TradeItem {
  id?: string;
  symbol?: string;
  side?: string;
  quantity: number;
  price: number;
  fee: number;
  pnl: number;
  timestamp?: string;
}

interface SessionMetrics {
  balance: Record<string, number>;
  total_pnl: number;
  win_rate: number;
  total_trades: number;
  open_positions: number;
}

interface SessionDetail {
  session_id: string;
  strategy_id: string;
  trading_mode: string;
  status: string;
  exchange_id: string;
  symbols: string[];
  timeframe: string;
  paper_capital: number | null;
  started_at: string | null;
  stopped_at: string | null;
  error_message: string | null;
  metrics: SessionMetrics;
  positions: PositionItem[];
  trades: TradeItem[];
}

/* ---------- Helpers ---------- */

function fmtUsd(v: number): string {
  const sign = v >= 0 ? '+' : '-';
  return `${sign}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
}

function fmtPrice(v: number): string {
  return `$${v.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
}

/* ---------- Position table ---------- */

interface PosRow {
  id: string;
  symbol: string;
  direction: string;
  quantity: string;
  avg_entry_price: string;
  unrealized_pnl: number;
  unrealized_pnl_fmt: string;
}

const positionColumns = [
  { key: 'symbol', header: 'Symbol' },
  {
    key: 'direction',
    header: 'Direction',
    render: (row: PosRow) => (
      <StatusBadge
        status={row.direction}
        variant={row.direction === 'long' ? 'success' : 'error'}
        size="sm"
      />
    ),
  },
  { key: 'quantity', header: 'Quantity' },
  { key: 'avg_entry_price', header: 'Entry Price' },
  {
    key: 'unrealized_pnl',
    header: 'Unrealized PnL',
    render: (row: PosRow) => (
      <span className={row.unrealized_pnl >= 0 ? 'text-status-success font-medium' : 'text-status-error font-medium'}>
        {row.unrealized_pnl_fmt}
      </span>
    ),
  },
];

/* ---------- Trade table ---------- */

interface TradeRow {
  id: string;
  side: string;
  symbol: string;
  price: string;
  quantity: string;
  fee: string;
  pnl: number;
  pnl_fmt: string;
  time: string;
}

const tradeColumns = [
  { key: 'id', header: '#', render: (row: TradeRow) => <span className="text-text-muted">{row.id}</span> },
  {
    key: 'side',
    header: 'Side',
    render: (row: TradeRow) => (
      <StatusBadge
        status={row.side}
        variant={row.side.toLowerCase() === 'buy' ? 'success' : 'error'}
        size="sm"
      />
    ),
  },
  { key: 'symbol', header: 'Symbol' },
  { key: 'price', header: 'Price' },
  { key: 'quantity', header: 'Qty', hideOnMobile: true },
  { key: 'fee', header: 'Fee', hideOnMobile: true },
  {
    key: 'pnl',
    header: 'PnL',
    render: (row: TradeRow) => (
      <span className={row.pnl >= 0 ? 'text-status-success font-medium' : 'text-status-error font-medium'}>
        {row.pnl_fmt}
      </span>
    ),
  },
  { key: 'time', header: 'Time' },
];

/* ---------- Page ---------- */

export default function SessionDetailPage() {
  const params = useParams();
  const router = useRouter();
  const { toast } = useToast();
  const sessionId = params.sessionId as string;

  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [stopping, setStopping] = useState(false);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchDetail = useCallback(async () => {
    try {
      const d = await fetchApi<SessionDetail>(`/api/trading/sessions/${sessionId}/detail`);
      setDetail(d);
    } catch (err) {
      logger.warn('SessionDetail', 'Failed to fetch session detail', err);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    fetchDetail();
  }, [fetchDetail]);

  // Poll every 5s if session is running
  useEffect(() => {
    if (detail?.status === 'running') {
      logger.info('SessionDetail', 'Polling started');
      intervalRef.current = setInterval(fetchDetail, 5000);
    }
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [detail?.status, fetchDetail]);

  const handleStop = async () => {
    setStopping(true);
    try {
      await fetchApi(`/api/trading/sessions/${sessionId}`, { method: 'DELETE' });
      toast('success', 'Session stopped');
      fetchDetail();
    } catch (err) {
      logger.error('SessionDetail', 'Failed to stop session', err);
      toast('error', 'Failed to stop session');
    } finally {
      setStopping(false);
    }
  };

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-text-muted">Loading session details...</p>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="flex flex-col items-center justify-center gap-4 py-20">
        <p className="text-sm text-text-muted">Session not found</p>
        <Button variant="outline" onClick={() => router.push('/strategies')}>
          <ArrowLeft className="h-4 w-4" /> Back to Strategies
        </Button>
      </div>
    );
  }

  const modeBadge = detail.trading_mode === 'paper' ? 'Paper' : 'Live';
  const modeVariant = detail.trading_mode === 'paper' ? 'info' : 'warning';
  const statusVariant = detail.status === 'running' ? 'success' : detail.status === 'error' ? 'error' : 'neutral';

  const posRows: PosRow[] = detail.positions.map((p, i) => ({
    id: String(i),
    symbol: p.symbol,
    direction: p.direction,
    quantity: p.quantity.toFixed(8).replace(/0+$/, '').replace(/\.$/, ''),
    avg_entry_price: fmtPrice(p.avg_entry_price),
    unrealized_pnl: p.unrealized_pnl,
    unrealized_pnl_fmt: fmtUsd(p.unrealized_pnl),
  }));

  const tradeRows: TradeRow[] = detail.trades.map((t, i) => ({
    id: t.id ?? String(i + 1),
    side: t.side ?? 'unknown',
    symbol: t.symbol ?? detail.symbols[0] ?? '',
    price: fmtPrice(t.price),
    quantity: t.quantity.toFixed(8).replace(/0+$/, '').replace(/\.$/, ''),
    fee: t.fee > 0 ? `$${t.fee.toFixed(4)}` : '\u2014',
    pnl: t.pnl,
    pnl_fmt: fmtUsd(t.pnl),
    time: t.timestamp ? new Date(t.timestamp).toLocaleString() : '\u2014',
  }));

  const balanceDisplay = Object.entries(detail.metrics.balance)
    .map(([k, v]) => `${v.toLocaleString('en-US', { minimumFractionDigits: 2 })} ${k}`)
    .join(', ') || '\u2014';

  return (
    <div className="flex flex-col gap-6">
      {/* Error banner */}
      {detail.status === 'error' && detail.error_message && (
        <div className="flex items-center gap-3 rounded-xl border border-status-error/30 bg-status-error/10 px-4 py-3">
          <AlertTriangle className="h-5 w-5 shrink-0 text-status-error" />
          <div>
            <p className="text-sm font-medium text-text-primary">Session Error</p>
            <p className="text-xs text-text-muted">{detail.error_message}</p>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="sm" onClick={() => router.push('/strategies')}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <h1 className="text-lg font-semibold text-text-primary">
              {detail.strategy_id}
            </h1>
            <p className="text-xs text-text-muted">
              {detail.exchange_id} &middot; {detail.symbols.join(', ')} &middot; {detail.timeframe}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge status={modeBadge} variant={modeVariant} />
          <StatusBadge status={detail.status.charAt(0).toUpperCase() + detail.status.slice(1)} variant={statusVariant} />
          {detail.status === 'running' && (
            <Button variant="danger" size="sm" onClick={handleStop} loading={stopping}>
              <Square className="h-3 w-3" /> Stop
            </Button>
          )}
        </div>
      </div>

      {/* Metrics row */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Balance</p>
          <p className="text-lg font-bold text-text-primary">{balanceDisplay}</p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Total PnL</p>
          <p className={`text-lg font-bold ${detail.metrics.total_pnl >= 0 ? 'text-status-success' : 'text-status-error'}`}>
            {fmtUsd(detail.metrics.total_pnl)}
          </p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Win Rate</p>
          <p className="text-lg font-bold text-text-primary">{detail.metrics.win_rate.toFixed(1)}%</p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Total Trades</p>
          <p className="text-lg font-bold text-text-primary">{detail.metrics.total_trades}</p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Open Positions</p>
          <p className="text-lg font-bold text-text-primary">{detail.metrics.open_positions}</p>
        </DataCard>
      </div>

      {/* Open Positions */}
      {posRows.length > 0 && (
        <DataCard title="Open Positions" description={`${posRows.length} active`}>
          <Table
            columns={positionColumns}
            data={posRows}
            keyExtractor={(row) => row.id}
            emptyMessage="No open positions"
          />
        </DataCard>
      )}

      {/* Trade History */}
      <DataCard title="Trade History" description={`${tradeRows.length} fills`}>
        <Table
          columns={tradeColumns}
          data={tradeRows}
          keyExtractor={(row) => row.id}
          emptyMessage="No trades recorded"
        />
      </DataCard>
    </div>
  );
}
