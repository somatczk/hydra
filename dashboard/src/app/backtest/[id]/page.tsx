'use client';

import { useEffect, useState, useMemo } from 'react';
import { useParams, useRouter } from 'next/navigation';
import {
  ArrowLeft,
  Pencil,
  Trash2,
  CheckCircle,
  XCircle,
  AlertTriangle,
  Share2,
} from 'lucide-react';
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
} from 'recharts';
import { Button } from '@/components/ui/Button';
import { DataCard } from '@/components/ui/DataCard';
import { Input } from '@/components/ui/Input';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { logger } from '@/lib/logger';
import {
  type TimeResolution,
  type EquityPoint,
  aggregateEquityCurve,
  formatXTick,
  formatYTick,
} from '../chart-utils';

/* ---------- Types ---------- */

interface ApiBacktestDetail {
  id: string;
  strategy: string;
  period: string;
  status: string;
  name: string;
  stopped_reason: string | null;
  metrics: {
    total_trades: number;
    win_rate: number;
    total_pnl: number;
    max_drawdown: number;
    sharpe_ratio: number;
    alpha?: number;
    beta?: number;
    max_consecutive_wins?: number;
    max_consecutive_losses?: number;
    expectancy?: number;
  };
  equity_curve: EquityPoint[];
  benchmark_equity?: EquityPoint[];
  trades: Array<{
    entry_time: string;
    exit_time: string;
    side: string;
    entry_price: number;
    exit_price: number;
    pnl: number;
  }>;
  transactions: Array<{
    trade_id: number;
    type: string;
    time: string;
    side: string;
    price: number;
    quantity: number;
    fee: number;
    pnl: number | null;
  }>;
}

interface Verification {
  trade_count_match: boolean;
  win_rate_match: boolean;
  total_pnl_match: boolean;
  computed_trade_count: number;
  computed_win_rate: number;
  computed_total_pnl: number;
  reported_trade_count: number;
  reported_win_rate: number;
  reported_total_pnl: number;
  all_passed: boolean;
}

interface TxnRow {
  id: string;
  tradeId: number;
  type: string;
  time: string;
  side: string;
  price: string;
  quantity: string;
  fee: string;
  pnl: number | null;
  pnlFormatted: string;
  isOpen: boolean;
}

interface MergedEquityPoint {
  timestamp: string;
  value: number;
  benchmark?: number;
}

/* ---------- Page ---------- */

export default function BacktestDetailPage() {
  const params = useParams();
  const router = useRouter();
  const { toast } = useToast();
  const resultId = params.id as string;

  const [detail, setDetail] = useState<ApiBacktestDetail | null>(null);
  const [verification, setVerification] = useState<Verification | null>(null);
  const [loading, setLoading] = useState(true);
  const [resolution, setResolution] = useState<TimeResolution>('days');
  const [isDark, setIsDark] = useState(false);

  // Rename state
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState('');

  // Delete state
  const [confirmDelete, setConfirmDelete] = useState(false);

  // Share state
  const [sharing, setSharing] = useState(false);

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains('dark'));
  }, []);

  useEffect(() => {
    async function load() {
      try {
        const [d, v] = await Promise.all([
          fetchApi<ApiBacktestDetail>(`/api/backtest/results/${resultId}`),
          fetchApi<Verification>(`/api/backtest/results/${resultId}/verify`).catch(() => null),
        ]);
        setDetail(d);
        setEditName(d.name || '');
        setVerification(v);
      } catch (err) {
        logger.warn('BacktestDetail', 'Failed to load backtest', err);
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [resultId]);

  const handleRename = async () => {
    if (!detail) return;
    try {
      await fetchApi(`/api/backtest/results/${resultId}`, {
        method: 'PATCH',
        body: JSON.stringify({ name: editName }),
      });
      setDetail({ ...detail, name: editName });
      setEditing(false);
      toast('success', 'Backtest renamed');
    } catch {
      toast('error', 'Failed to rename backtest');
    }
  };

  const handleDelete = async () => {
    try {
      await fetchApi<undefined>(`/api/backtest/results/${resultId}`, { method: 'DELETE' });
      toast('success', 'Backtest deleted');
      router.push('/backtest');
    } catch {
      toast('error', 'Failed to delete backtest');
    }
  };

  const handleShare = async () => {
    setSharing(true);
    try {
      const res = await fetchApi<{ share_url: string }>(`/api/backtest/results/${resultId}/share`, {
        method: 'POST',
      });
      await navigator.clipboard.writeText(res.share_url);
      toast('success', 'Share link copied to clipboard');
    } catch {
      toast('error', 'Failed to create share link');
    } finally {
      setSharing(false);
    }
  };

  const accentColor = isDark ? '#2383e2' : '#2563eb';
  const benchmarkColor = isDark ? 'rgba(255,255,255,0.3)' : '#94a3b8';
  const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)';
  const textMuted = isDark ? 'rgba(255,255,255,0.44)' : '#475569';

  const hasBenchmark = detail?.benchmark_equity && detail.benchmark_equity.length > 0;

  // Merge equity + benchmark into single dataset for the chart
  const chartData: MergedEquityPoint[] = useMemo(() => {
    if (!detail) return [];
    const aggregated = aggregateEquityCurve(detail.equity_curve, resolution);
    if (!hasBenchmark) return aggregated;

    const benchAggregated = aggregateEquityCurve(detail.benchmark_equity!, resolution);
    const benchMap = new Map(benchAggregated.map((p) => [p.timestamp, p.value]));
    return aggregated.map((p) => ({
      ...p,
      benchmark: benchMap.get(p.timestamp),
    }));
  }, [detail, resolution, hasBenchmark]);

  const CustomTooltip = useMemo(() => {
    return function ChartTooltip({ active, payload, label }: { active?: boolean; payload?: Array<{ value: number; dataKey: string }>; label?: string }) {
      if (!active || !payload?.length) return null;
      return (
        <div className="rounded-lg border border-border-default bg-bg-elevated px-3 py-2 shadow-lg">
          <p className="text-xs text-text-muted">{label ? formatXTick(label, resolution) : ''}</p>
          {payload.map((entry) => (
            <p key={entry.dataKey} className="text-sm font-medium text-text-primary">
              {entry.dataKey === 'benchmark' ? 'Benchmark' : 'Strategy'}: ${entry.value.toLocaleString('en-US', { minimumFractionDigits: 2 })}
            </p>
          ))}
        </div>
      );
    };
  }, [resolution]);

  const txnRows: TxnRow[] = useMemo(() => {
    if (!detail) return [];
    const txns = detail.transactions ?? [];
    // Determine which trade_ids have an exit (= closed)
    const exitIds = new Set(txns.filter(t => t.type === 'exit').map(t => t.trade_id));
    return txns.map((t, i) => ({
      id: String(i),
      tradeId: t.trade_id,
      type: t.type,
      time: new Date(t.time).toLocaleString(),
      side: t.side,
      price: `$${t.price.toLocaleString('en-US', { minimumFractionDigits: 2 })}`,
      quantity: t.quantity.toFixed(8).replace(/0+$/, '').replace(/\.$/, ''),
      fee: t.fee > 0 ? `$${t.fee.toFixed(4)}` : '\u2014',
      pnl: t.pnl,
      pnlFormatted: t.pnl != null
        ? `${t.pnl >= 0 ? '+' : '-'}$${Math.abs(t.pnl).toLocaleString('en-US', { minimumFractionDigits: 2 })}`
        : '\u2014',
      isOpen: t.type === 'entry' && !exitIds.has(t.trade_id),
    }));
  }, [detail]);

  const txnColumns = [
    {
      key: 'tradeId',
      header: '#',
      render: (row: TxnRow) => <span className="text-text-muted">#{row.tradeId}</span>,
    },
    {
      key: 'type',
      header: 'Type',
      render: (row: TxnRow) => (
        <StatusBadge
          status={row.isOpen ? 'Open' : row.type === 'entry' ? 'Entry' : 'Exit'}
          variant={row.isOpen ? 'warning' : row.type === 'entry' ? 'info' : 'neutral'}
          size="sm"
        />
      ),
    },
    { key: 'time', header: 'Time' },
    {
      key: 'side',
      header: 'Side',
      render: (row: TxnRow) => (
        <StatusBadge
          status={row.side}
          variant={row.side === 'Buy' ? 'success' : 'error'}
          size="sm"
        />
      ),
    },
    { key: 'price', header: 'Price' },
    { key: 'quantity', header: 'Qty', hideOnMobile: true },
    { key: 'fee', header: 'Fee', hideOnMobile: true },
    {
      key: 'pnl',
      header: 'PnL',
      render: (row: TxnRow) => row.pnl != null ? (
        <span className={row.pnl >= 0 ? 'text-status-success font-medium' : 'text-status-error font-medium'}>
          {row.pnlFormatted}
        </span>
      ) : <span className="text-text-muted">{'\u2014'}</span>,
    },
  ];

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-text-muted">Loading backtest details...</p>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="flex flex-col items-center justify-center gap-4 py-20">
        <p className="text-sm text-text-muted">Backtest not found</p>
        <Button variant="outline" onClick={() => router.push('/backtest')}>
          <ArrowLeft className="h-4 w-4" /> Back to Backtests
        </Button>
      </div>
    );
  }

  const { metrics } = detail;

  return (
    <div className="flex flex-col gap-6">
      {/* Stopped reason banner */}
      {detail.stopped_reason && (
        <div className="flex items-center gap-3 rounded-xl border border-status-warning/30 bg-status-warning/10 px-4 py-3">
          <AlertTriangle className="h-5 w-5 shrink-0 text-status-warning" />
          <div>
            <p className="text-sm font-medium text-text-primary">Backtest stopped early</p>
            <p className="text-xs text-text-muted">
              {detail.stopped_reason === 'margin_call'
                ? 'Margin call \u2014 equity reached zero'
                : detail.stopped_reason}
            </p>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="sm" onClick={() => router.push('/backtest')}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            {editing ? (
              <div className="flex items-center gap-2">
                <Input
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  className="h-8 w-56"
                  autoFocus
                  onKeyDown={(e) => { if (e.key === 'Enter') handleRename(); if (e.key === 'Escape') setEditing(false); }}
                />
                <Button variant="primary" size="sm" onClick={handleRename}>Save</Button>
                <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>Cancel</Button>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <h1 className="text-lg font-semibold text-text-primary">
                  {detail.name || detail.strategy}
                </h1>
                <button onClick={() => { setEditName(detail.name || detail.strategy); setEditing(true); }} className="text-text-muted hover:text-text-primary">
                  <Pencil className="h-3.5 w-3.5" />
                </button>
              </div>
            )}
            <p className="text-xs text-text-muted">{detail.strategy} &middot; {detail.period}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge status={detail.status === 'completed' ? 'Completed' : detail.status} variant={detail.status === 'completed' ? 'success' : 'info'} />
          <Button variant="outline" size="sm" onClick={handleShare} loading={sharing}>
            <Share2 className="h-4 w-4" /> Share
          </Button>
          {confirmDelete ? (
            <div className="flex items-center gap-2 rounded-lg border border-status-error/30 bg-status-error/5 px-3 py-1.5">
              <span className="text-xs text-text-primary">Delete this backtest?</span>
              <Button variant="danger" size="sm" onClick={handleDelete}>Confirm</Button>
              <Button variant="ghost" size="sm" onClick={() => setConfirmDelete(false)}>Cancel</Button>
            </div>
          ) : (
            <Button variant="outline" size="sm" onClick={() => setConfirmDelete(true)}>
              <Trash2 className="h-4 w-4" /> Delete
            </Button>
          )}
        </div>
      </div>

      {/* Metrics row */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Total PnL</p>
          <p className={`text-lg font-bold ${metrics.total_pnl >= 0 ? 'text-status-success' : 'text-status-error'}`}>
            {metrics.total_pnl >= 0 ? '+' : '-'}${Math.abs(metrics.total_pnl).toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Win Rate</p>
          <p className="text-lg font-bold text-text-primary">{metrics.win_rate.toFixed(1)}%</p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Sharpe Ratio</p>
          <p className="text-lg font-bold text-text-primary">{metrics.sharpe_ratio.toFixed(2)}</p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Max Drawdown</p>
          <p className="text-lg font-bold text-status-error">{metrics.max_drawdown.toFixed(1)}%</p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Total Trades</p>
          <p className="text-lg font-bold text-text-primary">{metrics.total_trades}</p>
        </DataCard>
      </div>

      {/* Extended metrics row */}
      {(metrics.alpha !== undefined || metrics.beta !== undefined || metrics.max_consecutive_wins !== undefined || metrics.max_consecutive_losses !== undefined || metrics.expectancy !== undefined) && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {metrics.alpha !== undefined && (
            <DataCard padding="sm">
              <p className="text-xs text-text-muted">Alpha</p>
              <p className={`text-lg font-bold ${metrics.alpha >= 0 ? 'text-status-success' : 'text-status-error'}`}>
                {metrics.alpha >= 0 ? '+' : ''}{metrics.alpha.toFixed(2)}
              </p>
            </DataCard>
          )}
          {metrics.beta !== undefined && (
            <DataCard padding="sm">
              <p className="text-xs text-text-muted">Beta</p>
              <p className="text-lg font-bold text-text-primary">{metrics.beta.toFixed(2)}</p>
            </DataCard>
          )}
          {metrics.max_consecutive_wins !== undefined && (
            <DataCard padding="sm">
              <p className="text-xs text-text-muted">Max Consec. Wins</p>
              <p className="text-lg font-bold text-status-success">{metrics.max_consecutive_wins}</p>
            </DataCard>
          )}
          {metrics.max_consecutive_losses !== undefined && (
            <DataCard padding="sm">
              <p className="text-xs text-text-muted">Max Consec. Losses</p>
              <p className="text-lg font-bold text-status-error">{metrics.max_consecutive_losses}</p>
            </DataCard>
          )}
          {metrics.expectancy !== undefined && (
            <DataCard padding="sm">
              <p className="text-xs text-text-muted">Expectancy</p>
              <p className={`text-lg font-bold ${metrics.expectancy >= 0 ? 'text-status-success' : 'text-status-error'}`}>
                {metrics.expectancy >= 0 ? '+' : ''}${metrics.expectancy.toFixed(2)}
              </p>
            </DataCard>
          )}
        </div>
      )}

      {/* Equity curve chart with optional benchmark overlay */}
      <DataCard title="Equity Curve">
        <div className="flex items-center gap-1 mb-3">
          {(['hours', 'days', 'months'] as TimeResolution[]).map((r) => (
            <Button
              key={r}
              size="sm"
              variant={resolution === r ? 'outline' : 'ghost'}
              onClick={() => setResolution(r)}
            >
              {r.charAt(0).toUpperCase() + r.slice(1)}
            </Button>
          ))}
        </div>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="detailGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={accentColor} stopOpacity={0.2} />
                  <stop offset="95%" stopColor={accentColor} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke={gridColor} vertical={false} />
              <XAxis
                dataKey="timestamp"
                tickFormatter={(ts) => formatXTick(ts, resolution)}
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
              {hasBenchmark && <Legend />}
              <Area
                type="monotone"
                dataKey="value"
                name="Strategy"
                stroke={accentColor}
                strokeWidth={2}
                fill="url(#detailGradient)"
                dot={false}
                activeDot={{ r: 4, fill: accentColor, strokeWidth: 0 }}
              />
              {hasBenchmark && (
                <Area
                  type="monotone"
                  dataKey="benchmark"
                  name="Benchmark"
                  stroke={benchmarkColor}
                  strokeWidth={1.5}
                  strokeDasharray="5 3"
                  fill="none"
                  dot={false}
                  activeDot={{ r: 3, fill: benchmarkColor, strokeWidth: 0 }}
                />
              )}
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </DataCard>

      {/* Verification panel */}
      {verification && (
        <DataCard title="Result Verification">
          <div className="flex flex-col gap-3">
            <div className="mb-2">
              {verification.all_passed ? (
                <StatusBadge status="All Checks Passed" variant="success" size="md" />
              ) : (
                <StatusBadge
                  status={`${[verification.trade_count_match, verification.win_rate_match, verification.total_pnl_match].filter(v => !v).length} of 3 Checks Failed`}
                  variant="error"
                  size="md"
                />
              )}
            </div>
            <div className="grid grid-cols-1 gap-2 text-sm">
              {([
                { label: 'Trade Count', reported: verification.reported_trade_count, computed: verification.computed_trade_count, match: verification.trade_count_match },
                { label: 'Win Rate', reported: `${verification.reported_win_rate.toFixed(1)}%`, computed: `${verification.computed_win_rate.toFixed(1)}%`, match: verification.win_rate_match },
                { label: 'Total PnL', reported: `$${verification.reported_total_pnl.toFixed(2)}`, computed: `$${verification.computed_total_pnl.toFixed(2)}`, match: verification.total_pnl_match },
              ] as const).map((row) => (
                <div key={row.label} className="flex items-center justify-between rounded-lg bg-bg-secondary px-3 py-2">
                  <span className="text-text-muted">{row.label}</span>
                  <div className="flex items-center gap-4">
                    <span className="text-text-primary">Reported: <span className="font-medium">{row.reported}</span></span>
                    <span className="text-text-primary">Computed: <span className="font-medium">{row.computed}</span></span>
                    {row.match ? (
                      <CheckCircle className="h-4 w-4 text-status-success" />
                    ) : (
                      <XCircle className="h-4 w-4 text-status-error" />
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </DataCard>
      )}

      {/* Transactions table */}
      <DataCard title="Transactions" description={`${txnRows.length} fills recorded`}>
        <Table
          columns={txnColumns}
          data={txnRows}
          keyExtractor={(row) => row.id}
          emptyMessage="No transactions recorded"
        />
      </DataCard>
    </div>
  );
}
