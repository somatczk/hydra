'use client';

import { useEffect, useState, useMemo, useCallback } from 'react';
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
import type { TooltipContentProps, TooltipValueType } from 'recharts';
import { Button } from '@/components/ui/Button';
import { DataCard } from '@/components/ui/DataCard';
import { Input } from '@/components/ui/Input';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { ResearchNav } from '@/components/research/ResearchNav';
import { ProvenanceBadge, ProvenancePanel, type DataProvenance } from '@/components/backtest/DataProvenance';
import { describeApiError, stateFromError } from '@/lib/apiState';
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
  strategy_id?: string | null;
  initial_capital?: number | null;
  provenance?: DataProvenance | null;
  stopped_reason: string | null;
  metrics: {
    total_trades: number;
    win_rate: number;
    total_pnl: number;
    max_drawdown: number;
    sharpe_ratio: number;
    alpha?: number | null;
    beta?: number | null;
    max_consecutive_wins?: number | null;
    max_consecutive_losses?: number | null;
    expectancy?: number | null;
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
  const [verification, setVerification] = useState<Verification | 'unavailable' | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
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
          fetchApi<Verification>(`/api/backtest/results/${resultId}/verify`).catch(() => 'unavailable' as const),
        ]);
        setDetail(d);
        setEditName(d.name || '');
        setVerification(v);
      } catch (err) {
        logger.warn('BacktestDetail', 'Failed to load backtest', err);
        setLoadError(err);
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
    } catch (err) {
      toast('error', describeApiError(err, 'Failed to rename backtest'));
    }
  };

  const handleDelete = async () => {
    try {
      await fetchApi<undefined>(`/api/backtest/results/${resultId}`, { method: 'DELETE' });
      toast('success', 'Backtest deleted');
      router.push('/backtest');
    } catch (err) {
      toast('error', describeApiError(err, 'Failed to delete backtest'));
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

  // a render function, not a component: recharts calls it with the hover state
  const renderTooltip = useCallback(({ active, payload, label }: TooltipContentProps<TooltipValueType, string | number>) => {
    if (!active || !payload?.length) return null;
    return (
      <div className="rounded-lg border border-line bg-surface px-3 py-2 shadow-lg">
        <p className="text-xs text-ink-2">{label != null ? formatXTick(String(label), resolution) : ''}</p>
        {payload.map((entry) => (
          <p key={String(entry.dataKey)} className="text-sm font-medium text-ink-1">
            {entry.dataKey === 'benchmark' ? 'Benchmark' : 'Strategy'}: ${Number(entry.value).toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </p>
        ))}
      </div>
    );
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
      render: (row: TxnRow) => <span className="text-ink-2">#{row.tradeId}</span>,
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
        <span className={row.pnl >= 0 ? 'text-ink-1 font-medium' : 'text-danger font-medium'}>
          {row.pnlFormatted}
        </span>
      ) : <span className="text-ink-2">{'\u2014'}</span>,
    },
  ];

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-ink-2">Loading backtest details...</p>
      </div>
    );
  }

  if (!detail) {
    const missing = loadError === null || stateFromError(loadError) === 'not_found';
    return (
      <div className="flex flex-col items-center justify-center gap-4 py-20">
        {missing ? (
          <p className="text-sm text-ink-2">Backtest not found</p>
        ) : (
          <DataStateErrorCard error={loadError} fallback="Failed to load backtest" />
        )}
        <Button variant="outline" onClick={() => router.push('/backtest')}>
          <ArrowLeft className="h-4 w-4" /> Back to Backtests
        </Button>
      </div>
    );
  }

  const { metrics } = detail;

  return (
    <div className="flex flex-col gap-6">
      <ResearchNav current="/backtest" />
      {/* Stopped reason banner */}
      {detail.stopped_reason && (
        <div className="flex items-center gap-3 rounded-xl border border-status-warning/30 bg-caution/10 px-4 py-3">
          <AlertTriangle className="h-5 w-5 shrink-0 text-caution" />
          <div>
            <p className="text-sm font-medium text-ink-1">Backtest stopped early</p>
            <p className="text-xs text-ink-2">
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
                <h1 className="text-lg font-semibold text-ink-1">
                  {detail.name || detail.strategy}
                </h1>
                <button onClick={() => { setEditName(detail.name || detail.strategy); setEditing(true); }} className="text-ink-2 hover:text-ink-1">
                  <Pencil className="h-3.5 w-3.5" />
                </button>
              </div>
            )}
            <p className="text-xs text-ink-2">{detail.strategy} &middot; {detail.period}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <ProvenanceBadge provenance={detail.provenance} />
          <StatusBadge status={detail.status === 'completed' ? 'Completed' : detail.status} variant={detail.status === 'completed' ? 'success' : 'info'} />
          <Button variant="outline" size="sm" onClick={handleShare} loading={sharing}>
            <Share2 className="h-4 w-4" /> Share
          </Button>
          {confirmDelete ? (
            <div className="flex items-center gap-2 rounded-lg border border-danger bg-danger-soft px-3 py-1.5">
              <span className="text-xs text-ink-1">Delete this backtest?</span>
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

      <ProvenancePanel provenance={detail.provenance} />

      {/* Metrics row */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <DataCard padding="sm">
          <p className="text-xs text-ink-2">Total PnL</p>
          <p className={`text-lg font-bold ${metrics.total_pnl >= 0 ? 'text-ink-1' : 'text-danger'}`}>
            {metrics.total_pnl >= 0 ? '+' : '-'}${Math.abs(metrics.total_pnl).toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-ink-2">Win Rate</p>
          <p className="text-lg font-bold text-ink-1">{metrics.win_rate.toFixed(1)}%</p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-ink-2">Sharpe Ratio</p>
          <p className="text-lg font-bold text-ink-1">{metrics.sharpe_ratio.toFixed(2)}</p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-ink-2">Max Drawdown</p>
          <p className="text-lg font-bold text-danger">{metrics.max_drawdown.toFixed(1)}%</p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-ink-2">Total Trades</p>
          <p className="text-lg font-bold text-ink-1">{metrics.total_trades}</p>
        </DataCard>
      </div>

      {/* Extended metrics row */}
      {(metrics.alpha != null || metrics.beta != null || metrics.max_consecutive_wins != null || metrics.max_consecutive_losses != null || metrics.expectancy != null) && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {metrics.alpha != null && (
            <DataCard padding="sm">
              <p className="text-xs text-ink-2">Alpha</p>
              <p className={`text-lg font-bold ${metrics.alpha >= 0 ? 'text-ink-1' : 'text-danger'}`}>
                {metrics.alpha >= 0 ? '+' : ''}{metrics.alpha.toFixed(2)}
              </p>
            </DataCard>
          )}
          {metrics.beta != null && (
            <DataCard padding="sm">
              <p className="text-xs text-ink-2">Beta</p>
              <p className="text-lg font-bold text-ink-1">{metrics.beta.toFixed(2)}</p>
            </DataCard>
          )}
          {metrics.max_consecutive_wins != null && (
            <DataCard padding="sm">
              <p className="text-xs text-ink-2">Max Consec. Wins</p>
              <p className="text-lg font-bold text-ink-1">{metrics.max_consecutive_wins}</p>
            </DataCard>
          )}
          {metrics.max_consecutive_losses != null && (
            <DataCard padding="sm">
              <p className="text-xs text-ink-2">Max Consec. Losses</p>
              <p className="text-lg font-bold text-danger">{metrics.max_consecutive_losses}</p>
            </DataCard>
          )}
          {metrics.expectancy != null && (
            <DataCard padding="sm">
              <p className="text-xs text-ink-2">Expectancy</p>
              <p className={`text-lg font-bold ${metrics.expectancy >= 0 ? 'text-ink-1' : 'text-danger'}`}>
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
              <Tooltip content={renderTooltip} />
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
      {verification === 'unavailable' && (
        <DataCard title="Result Verification">
          <div className="flex items-center gap-2 text-sm text-ink-2">
            <DataStateBadge state="unavailable" reason="The stored trades could not be re-read to verify the metrics" />
            Verification UNAVAILABLE: the metrics above are not confirmed against the stored trades.
          </div>
        </DataCard>
      )}
      {verification && verification !== 'unavailable' && (
        <DataCard title="Result Verification">
          <div className="flex flex-col gap-3">
            <div className="mb-2">
              {verification.all_passed ? (
                <StatusBadge status={`Metrics recomputed from ${verification.computed_trade_count} stored trades: match`} variant="success" size="md" />
              ) : (
                <StatusBadge
                  status={`Metrics recomputed from ${verification.computed_trade_count} stored trades: mismatch: ${[['trade count', verification.trade_count_match], ['win rate', verification.win_rate_match], ['total PnL', verification.total_pnl_match]].filter(([, ok]) => !ok).map(([name]) => name).join(', ')}`}
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
                <div key={row.label} className="flex items-center justify-between rounded-lg bg-surface px-3 py-2">
                  <span className="text-ink-2">{row.label}</span>
                  <div className="flex items-center gap-4">
                    <span className="text-ink-1">Reported: <span className="font-medium">{row.reported}</span></span>
                    <span className="text-ink-1">Computed: <span className="font-medium">{row.computed}</span></span>
                    {row.match ? (
                      <CheckCircle className="h-4 w-4 text-ink-1" />
                    ) : (
                      <XCircle className="h-4 w-4 text-danger" />
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
