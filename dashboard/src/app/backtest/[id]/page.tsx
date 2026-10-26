'use client';

import { useEffect, useState, useMemo, useCallback } from 'react';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import {
  ArrowLeft,
  Pencil,
  Trash2,
  CheckCircle,
  XCircle,
  AlertTriangle,
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
import { Readout } from '@/components/ui/Readout';
import { Section } from '@/components/ui/Section';
import { Input } from '@/components/ui/Input';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { fetchApi } from '@/lib/api';
import { money, signed } from '@/lib/format';
import { useToast } from '@/components/ui/Toast';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { ResearchNav } from '@/components/research/ResearchNav';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { StartSessionDialog } from '@/components/sessions/StartSessionDialog';
import {
  ProvenanceBadge,
  ProvenancePanel,
  formatUtc,
  type DataProvenance,
} from '@/components/backtest/DataProvenance';
import { describeApiError, stateFromError } from '@/lib/apiState';
import { logger } from '@/lib/logger';
import {
  type TimeResolution,
  type EquityPoint,
  aggregateEquityCurve,
  chartColors,
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

const usdt = (v: number) => `${money(v) ?? '—'} USDT`;

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

  // Rename state
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState('');

  // Delete state
  const [confirmDelete, setConfirmDelete] = useState(false);

  // Start paper session state
  const [starting, setStarting] = useState(false);

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
      setConfirmDelete(false);
      toast('success', 'Backtest deleted');
      router.push('/backtest');
    } catch (err) {
      toast('error', describeApiError(err, 'Failed to delete backtest'));
    }
  };

  const colors = chartColors();

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

  const chartSummary = useMemo(() => {
    const curve = detail?.equity_curve ?? [];
    if (curve.length === 0) return null;
    const first = curve[0];
    const last = curve[curve.length - 1];
    return `Equity from ${formatUtc(first.timestamp)} to ${formatUtc(last.timestamp)}: start ${usdt(first.value)}, end ${usdt(last.value)}`;
  }, [detail]);

  const failedVerifyFields = verification && verification !== 'unavailable'
    ? ([
        ['trade count', verification.trade_count_match],
        ['win rate', verification.win_rate_match],
        ['total PnL', verification.total_pnl_match],
      ] as const).filter(([, ok]) => !ok).map(([name]) => name)
    : [];

  // a render function, not a component: recharts calls it with the hover state
  const renderTooltip = useCallback(({ active, payload, label }: TooltipContentProps<TooltipValueType, string | number>) => {
    if (!active || !payload?.length) return null;
    return (
      <div className="rounded-lg border border-line bg-surface px-3 py-2 shadow-lg">
        <p className="text-xs text-ink-2">{label != null ? formatXTick(String(label), resolution) : ''}</p>
        {payload.map((entry) => (
          <p key={String(entry.dataKey)} className="text-sm font-medium text-ink-1">
            {entry.dataKey === 'benchmark' ? 'Benchmark' : 'Strategy'}: {usdt(Number(entry.value))}
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
      price: money(t.price) ?? '—',
      quantity: t.quantity.toFixed(8).replace(/0+$/, '').replace(/\.$/, ''),
      fee: t.fee > 0 ? t.fee.toFixed(4) : '\u2014',
      pnl: t.pnl,
      pnlFormatted: signed(t.pnl) ?? '—',
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
    { key: 'price', header: 'Price (USDT)', numeric: true },
    { key: 'quantity', header: 'Qty', hideOnMobile: true },
    { key: 'fee', header: 'Fee (USDT)', numeric: true, hideOnMobile: true },
    {
      key: 'pnl',
      header: 'PnL (USDT)',
      numeric: true,
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
  const hasExtendedMetrics = metrics.alpha != null || metrics.beta != null
    || metrics.max_consecutive_wins != null || metrics.max_consecutive_losses != null
    || metrics.expectancy != null;

  return (
    <div className="flex flex-col gap-6">
      <ResearchNav current="/backtest" />
      {/* Stopped reason banner */}
      {detail.stopped_reason && (
        <div className="flex items-center gap-3 rounded-lg border border-caution/30 bg-caution-soft px-4 py-3">
          <AlertTriangle className="h-5 w-5 shrink-0 text-caution" aria-hidden="true" />
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
                <button onClick={() => { setEditName(detail.name || detail.strategy); setEditing(true); }} className="text-ink-2 hover:text-ink-1" aria-label="Rename this backtest">
                  <Pencil className="h-3.5 w-3.5" />
                </button>
              </div>
            )}
            <p className="text-xs text-ink-2">{detail.strategy} &middot; {detail.period}</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <ProvenanceBadge provenance={detail.provenance} />
          <StatusBadge status={detail.status === 'completed' ? 'Completed' : detail.status} variant={detail.status === 'completed' ? 'success' : 'info'} />
          <Link
            href={`/backtest/compare?a=${resultId}`}
            className="inline-flex h-8 max-lg:h-11 items-center rounded border border-line-strong bg-surface px-3 text-sm font-medium text-ink-1 transition-colors hover:bg-hover"
          >
            Compare with…
          </Link>
          {detail.strategy_id && (
            <Button variant="outline" size="sm" onClick={() => setStarting(true)}>
              Start paper session
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={() => setConfirmDelete(true)}>
            <Trash2 className="h-4 w-4" aria-hidden="true" /> Delete
          </Button>
        </div>
      </div>

      <ProvenancePanel provenance={detail.provenance} />

      {/* Metrics row */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Readout
          label="Total PnL (USDT)"
          value={<span className={metrics.total_pnl < 0 ? 'text-danger' : undefined}>{signed(metrics.total_pnl)}</span>}
        />
        <Readout label="Win rate" value={`${metrics.win_rate.toFixed(1)}%`} />
        <Readout label="Sharpe" value={metrics.sharpe_ratio.toFixed(2)} />
        <Readout label="Max drawdown" value={<span className="text-danger">{metrics.max_drawdown.toFixed(1)}%</span>} />
        <Readout label="Trades" value={metrics.total_trades} />
      </div>

      {/* Extended metrics row */}
      {hasExtendedMetrics && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {metrics.alpha != null && (
            <Readout
              label="Alpha"
              value={<span className={metrics.alpha < 0 ? 'text-danger' : undefined}>{`${metrics.alpha >= 0 ? '+' : ''}${metrics.alpha.toFixed(2)}`}</span>}
            />
          )}
          {metrics.beta != null && (
            <Readout label="Beta" value={metrics.beta.toFixed(2)} />
          )}
          {metrics.max_consecutive_wins != null && (
            <Readout label="Max consec. wins" value={metrics.max_consecutive_wins} />
          )}
          {metrics.max_consecutive_losses != null && (
            <Readout label="Max consec. losses" value={<span className="text-danger">{metrics.max_consecutive_losses}</span>} />
          )}
          {metrics.expectancy != null && (
            <Readout
              label="Expectancy (USDT)"
              value={<span className={metrics.expectancy < 0 ? 'text-danger' : undefined}>{signed(metrics.expectancy)}</span>}
            />
          )}
        </div>
      )}

      {/* Equity curve chart with optional benchmark overlay */}
      <Section title="Equity curve">
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
        {chartSummary ? (
          <div className="h-64" role="img" aria-label={chartSummary}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="detailGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={colors.accent} stopOpacity={0.2} />
                    <stop offset="95%" stopColor={colors.accent} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke={colors.grid} vertical={false} />
                <XAxis
                  dataKey="timestamp"
                  tickFormatter={(ts) => formatXTick(ts, resolution)}
                  tick={{ fontSize: 11, fill: colors.text }}
                  axisLine={false}
                  tickLine={false}
                  minTickGap={40}
                />
                <YAxis
                  tickFormatter={formatYTick}
                  tick={{ fontSize: 11, fill: colors.text }}
                  axisLine={false}
                  tickLine={false}
                  width={44}
                  label={{ value: 'USDT', angle: -90, position: 'insideLeft', fill: colors.text, fontSize: 11 }}
                />
                <Tooltip content={renderTooltip} />
                {hasBenchmark && <Legend />}
                <Area
                  type="monotone"
                  dataKey="value"
                  name="Strategy"
                  stroke={colors.accent}
                  strokeWidth={2}
                  fill="url(#detailGradient)"
                  dot={false}
                  activeDot={{ r: 4, fill: colors.accent, strokeWidth: 0 }}
                />
                {hasBenchmark && (
                  <Area
                    type="monotone"
                    dataKey="benchmark"
                    name="Benchmark"
                    stroke={colors.muted}
                    strokeWidth={1.5}
                    strokeDasharray="5 3"
                    fill="none"
                    dot={false}
                    activeDot={{ r: 3, fill: colors.muted, strokeWidth: 0 }}
                  />
                )}
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-sm text-ink-2">No equity points recorded.</p>
        )}
      </Section>

      {/* Verification panel */}
      {verification === 'unavailable' && (
        <Section title="Result verification">
          <div className="flex items-center gap-2 text-sm text-ink-2">
            <DataStateBadge state="unavailable" reason="The stored trades could not be re-read to verify the metrics" />
            Verification UNAVAILABLE: the metrics above are not confirmed against the stored trades.
          </div>
        </Section>
      )}
      {verification && verification !== 'unavailable' && (
        <Section title="Result verification">
          <div className="flex flex-col gap-3">
            <div className="mb-2">
              {verification.all_passed ? (
                <StatusBadge status={`Metrics recomputed from ${verification.computed_trade_count} stored trades: match`} variant="success" size="md" />
              ) : (
                <StatusBadge
                  status={`Metrics recomputed from ${verification.computed_trade_count} stored trades: mismatch on ${failedVerifyFields.join(', ')}`}
                  variant="warning"
                  size="md"
                />
              )}
            </div>
            <div className="grid grid-cols-1 gap-2 text-sm">
              {([
                { label: 'Trade count', reported: verification.reported_trade_count, computed: verification.computed_trade_count, match: verification.trade_count_match },
                { label: 'Win rate', reported: `${verification.reported_win_rate.toFixed(1)}%`, computed: `${verification.computed_win_rate.toFixed(1)}%`, match: verification.win_rate_match },
                { label: 'Total PnL (USDT)', reported: signed(verification.reported_total_pnl) ?? '—', computed: signed(verification.computed_total_pnl) ?? '—', match: verification.total_pnl_match },
              ] as const).map((row) => (
                <div key={row.label} className="flex items-center justify-between rounded-lg bg-raised px-3 py-2">
                  <span className="text-ink-2">{row.label}</span>
                  <div className="flex items-center gap-4">
                    <span className="text-ink-1">Reported: <span className="font-medium">{row.reported}</span></span>
                    <span className="text-ink-1">Computed: <span className="font-medium">{row.computed}</span></span>
                    {row.match ? (
                      <CheckCircle className="h-4 w-4 text-ink-1" aria-hidden="true" />
                    ) : (
                      <XCircle className="h-4 w-4 text-danger" aria-hidden="true" />
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </Section>
      )}

      {/* Transactions table */}
      <Section title="Transactions" action={<span className="text-xs text-ink-2">{txnRows.length} fills recorded</span>}>
        <Table
          columns={txnColumns}
          data={txnRows}
          keyExtractor={(row) => row.id}
          emptyMessage="No transactions recorded"
        />
      </Section>

      {confirmDelete && (
        <ConfirmDialog
          open
          destructive
          title="Delete this backtest?"
          primaryLabel="Delete backtest"
          secondaryLabel="Keep it"
          pendingLabel="Deleting…"
          pendingHint="Closing this won't cancel the delete."
          onConfirm={handleDelete}
          onClose={() => setConfirmDelete(false)}
        >
          <p>The stored result and its trades are deleted for good.</p>
        </ConfirmDialog>
      )}

      {starting && detail.strategy_id && (
        <StartSessionDialog strategyId={detail.strategy_id} onClose={() => setStarting(false)} />
      )}
    </div>
  );
}
