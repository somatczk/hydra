'use client';

import { useEffect, useState, useCallback } from 'react';
import { Download, Search, Plus, X } from 'lucide-react';
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Cell,
} from 'recharts';
import { Readout } from '@/components/ui/Readout';
import { Section } from '@/components/ui/Section';
import { DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Table } from '@/components/ui/Table';
import { useToast } from '@/components/ui/Toast';
import { ApiError, fetchApi } from '@/lib/api';
import { describeApiError } from '@/lib/apiState';
import { sideLabel } from '@/lib/derive';
import { money, price, qty, signed, signedLabel, utc } from '@/lib/format';
import { logger } from '@/lib/logger';

/* ---------- Types ---------- */

interface PortfolioSummary {
  total_value: number;
  unrealized_pnl: number;
  realized_pnl: number;
  daily_pnl: number;
  max_drawdown_pct: number;
  total_fees: number;
  change_pct: number;
  paper_capital_pending?: number | null;
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

interface EquityPoint { timestamp: string; value: number; }
interface DailyPnl { date: string; pnl: number; }

interface MonthlyReturn {
  month: string;
  return_pct: number;
}

interface AttributionItem {
  strategy: string;
  pnl: number;
}

interface StrategyOption {
  id: string;
  name: string;
}

interface Trade {
  id: string;
  strategy_id: string;
  symbol: string;
  side: string;
  timestamp: string;
  price: number;
  quantity: number;
  pnl: number;
  notes: string;
  tags: string[];
}

interface TradesResponse {
  trades: Trade[];
  total: number;
  page: number;
  limit: number;
  pages: number;
}

/* ---------- Utilities ---------- */

function computeDrawdown(curve: EquityPoint[]): Array<{ timestamp: string; drawdown: number }> {
  let peak = 0;
  return curve.map((p) => {
    if (p.value > peak) peak = p.value;
    const dd = peak > 0 ? ((peak - p.value) / peak) * 100 : 0;
    return { timestamp: p.timestamp, drawdown: dd };
  });
}

function formatXTick(timestamp: string): string {
  const d = new Date(timestamp);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function formatYTick(value: number): string {
  return `$${(value / 1000).toFixed(0)}k`;
}

function formatDrawdownTick(value: number): string {
  return `${value.toFixed(1)}%`;
}

const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Text and fill together: each pair is in globals.test.ts PAIRS (≥ 4.5:1 in both themes). */
function getReturnColor(pct: number): string {
  if (pct > 5) return 'bg-ink-1 text-canvas';
  if (pct > -2) return 'bg-hover text-ink-1';
  if (pct > -5) return 'bg-danger-soft text-danger';
  return 'bg-danger text-canvas';
}

/* ---------- Trade Table Row ---------- */

type TradeRow = Trade;

function SignedValue({ value }: { value: number }) {
  return (
    <>
      <span aria-hidden="true">{signed(value) ?? '—'}</span>
      <span className="sr-only">{signedLabel(value) ?? 'not available'}</span>
    </>
  );
}

/* ---------- Page ---------- */

export default function PortfolioPage() {
  useEffect(() => { logger.info('Portfolio', 'Page mounted'); }, []);
  const { toast } = useToast();
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [equityCurve, setEquityCurve] = useState<EquityPoint[] | null>(null);
  const [dailyPnl, setDailyPnl] = useState<DailyPnl[]>([]);
  const [monthlyReturns, setMonthlyReturns] = useState<MonthlyReturn[] | null>(null);
  const [attribution, setAttribution] = useState<AttributionItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [source, setSource] = useState<'paper' | null>(null);
  const [storedMode, setStoredMode] = useState<string | null>(null);
  const [isDark, setIsDark] = useState(false);
  const [fetchErrors, setFetchErrors] = useState<Record<string, unknown>>({});

  // Trade journal state
  const [strategies, setStrategies] = useState<StrategyOption[]>([]);
  const [filterStrategy, setFilterStrategy] = useState('');
  const [filterSymbol, setFilterSymbol] = useState('');
  const [filterFromDate, setFilterFromDate] = useState('');
  const [filterToDate, setFilterToDate] = useState('');
  const [filterMinPnl, setFilterMinPnl] = useState('');
  const [filterMaxPnl, setFilterMaxPnl] = useState('');
  const [trades, setTrades] = useState<Trade[]>([]);
  const [tradesTotal, setTradesTotal] = useState(0);
  const [tradesPage, setTradesPage] = useState(1);
  const [tradesPages, setTradesPages] = useState(1);
  const [tradesLoading, setTradesLoading] = useState(false);

  // Inline edit state
  const [editingNoteId, setEditingNoteId] = useState<string | null>(null);
  const [editingNoteValue, setEditingNoteValue] = useState('');
  const [addingTagId, setAddingTagId] = useState<string | null>(null);
  const [newTagValue, setNewTagValue] = useState('');

  // Export state
  const [exportFormat, setExportFormat] = useState('generic');
  const [exportFromDate, setExportFromDate] = useState('');
  const [exportToDate, setExportToDate] = useState('');
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains('dark'));
  }, []);

  const loadData = useCallback(async () => {
    setLoading(true);
    setFetchErrors({});

    // No guessed source: without the platform config, source-specific widgets show why
    let source: 'paper' | null = null;
    let configError: unknown = null;
    try {
      const cfg = await fetchApi<{ trading_mode: string }>('/api/system/config');
      source = 'paper'; // live is refused until P3; Settings says the same
      setStoredMode(cfg.trading_mode);
    } catch (err) {
      logger.warn('Portfolio', 'Trading mode unknown; portfolio data not requested', err);
      configError = err ?? true;
    }
    setSource(source);
    const qs = `?source=${source}`;

    await Promise.all([
      ...(source === null
        ? []
        : [
            fetchApi<PortfolioSummary>(`/api/portfolio/summary${qs}`)
              .then(setSummary)
              .catch((err) => { setFetchErrors((prev) => ({ ...prev, summary: err ?? true })); }),
            fetchApi<Position[]>(`/api/portfolio/positions${qs}`)
              .then(setPositions)
              .catch((err) => { setFetchErrors((prev) => ({ ...prev, positions: err ?? true })); }),
            fetchApi<EquityPoint[]>(`/api/portfolio/equity-curve${qs}`)
              .then(setEquityCurve)
              .catch((err) => { setFetchErrors((prev) => ({ ...prev, equity: err ?? true })); }),
            fetchApi<DailyPnl[]>(`/api/portfolio/daily-pnl${qs}`)
              .then(setDailyPnl)
              .catch((err) => { setFetchErrors((prev) => ({ ...prev, dailyPnl: err ?? true })); }),
            fetchApi<MonthlyReturn[]>(`/api/portfolio/monthly-returns${qs}`)
              .then(setMonthlyReturns)
              .catch((err) => { setFetchErrors((prev) => ({ ...prev, monthlyReturns: err ?? true })); }),
            fetchApi<AttributionItem[]>(`/api/portfolio/attribution${qs}`)
              .then(setAttribution)
              .catch((err) => { setFetchErrors((prev) => ({ ...prev, attribution: err ?? true })); }),
          ]),
      fetchApi<StrategyOption[]>('/api/strategies')
        .then((data) => setStrategies(data.map((s) => ({ id: s.id, name: s.name }))))
        .catch(() => {}),
    ]);

    if (configError !== null) {
      setFetchErrors((prev) => ({
        ...prev,
        summary: configError,
        positions: configError,
        equity: configError,
        dailyPnl: configError,
        monthlyReturns: configError,
        attribution: configError,
      }));
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 30_000);
    return () => clearInterval(interval);
  }, [loadData]);

  // Fetch trades with filters
  const loadTrades = useCallback(async (page = 1) => {
    setTradesLoading(true);
    const params = new URLSearchParams();
    params.set('page', String(page));
    if (source) params.set('source', source);
    if (filterStrategy) params.set('strategy_id', filterStrategy);
    if (filterSymbol) params.set('symbol', filterSymbol);
    if (filterFromDate) params.set('from_date', filterFromDate);
    if (filterToDate) params.set('to_date', filterToDate);
    if (filterMinPnl) params.set('min_pnl', filterMinPnl);
    if (filterMaxPnl) params.set('max_pnl', filterMaxPnl);
    try {
      const res = await fetchApi<TradesResponse>(`/api/portfolio/trades?${params.toString()}`);
      setTrades(res.trades);
      setTradesTotal(res.total);
      setTradesPage(res.page);
      setTradesPages(res.pages);
    } catch (err) {
      toast('error', describeApiError(err, 'Failed to load trades'));
    } finally {
      setTradesLoading(false);
    }
  }, [source, filterStrategy, filterSymbol, filterFromDate, filterToDate, filterMinPnl, filterMaxPnl, toast]);

  const handleApplyFilters = () => {
    loadTrades(1);
  };

  // Inline note editing
  const handleNoteSave = async (tradeId: string) => {
    try {
      await fetchApi(`/api/portfolio/trades/${tradeId}`, {
        method: 'PATCH',
        body: JSON.stringify({ notes: editingNoteValue }),
      });
      setTrades((prev) => prev.map((t) => t.id === tradeId ? { ...t, notes: editingNoteValue } : t));
      setEditingNoteId(null);
    } catch {
      toast('error', 'Failed to save note');
    }
  };

  // Tag management
  const handleAddTag = async (tradeId: string) => {
    const tag = newTagValue.trim();
    if (!tag) return;
    const trade = trades.find((t) => t.id === tradeId);
    if (!trade) return;
    const updatedTags = [...trade.tags, tag];
    try {
      await fetchApi(`/api/portfolio/trades/${tradeId}`, {
        method: 'PATCH',
        body: JSON.stringify({ tags: updatedTags }),
      });
      setTrades((prev) => prev.map((t) => t.id === tradeId ? { ...t, tags: updatedTags } : t));
      setNewTagValue('');
      setAddingTagId(null);
    } catch {
      toast('error', 'Failed to add tag');
    }
  };

  const handleRemoveTag = async (tradeId: string, tagIndex: number) => {
    const trade = trades.find((t) => t.id === tradeId);
    if (!trade) return;
    const updatedTags = trade.tags.filter((_, i) => i !== tagIndex);
    try {
      await fetchApi(`/api/portfolio/trades/${tradeId}`, {
        method: 'PATCH',
        body: JSON.stringify({ tags: updatedTags }),
      });
      setTrades((prev) => prev.map((t) => t.id === tradeId ? { ...t, tags: updatedTags } : t));
    } catch {
      toast('error', 'Failed to remove tag');
    }
  };

  // Export handler
  const handleExport = async () => {
    setExporting(true);
    const params = new URLSearchParams();
    params.set('format', exportFormat);
    if (source) params.set('source', source);
    if (exportFromDate) params.set('from_date', exportFromDate);
    if (exportToDate) params.set('to_date', exportToDate);
    try {
      const url = `${process.env.NEXT_PUBLIC_API_URL || window.location.origin}/api/portfolio/export/csv?${params.toString()}`;
      const res = await fetch(url);
      if (!res.ok) throw new ApiError(res.status, await res.text());
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `trades_${exportFormat}_${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
      toast('success', 'Export downloaded');
    } catch (err) {
      toast('error', describeApiError(err, 'Failed to export trades'));
    } finally {
      setExporting(false);
    }
  };

  // Derive allocation from API positions
  const allocationItems = positions.length > 0 && summary
    ? positions.map((p) => {
        const posValue = p.size * p.current_price;
        const allocation = Math.round((posValue / summary.total_value) * 100);
        return {
          label: `${p.pair} ${p.side}`,
          allocation,
          value: `$${posValue.toLocaleString('en-US', { minimumFractionDigits: 2 })}`,
        };
      })
    : [];

  const drawdownData = equityCurve ? computeDrawdown(equityCurve) : [];

  const accentColor = isDark ? '#2383e2' : '#2563eb';
  const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)';
  const textMuted = isDark ? 'rgba(255,255,255,0.44)' : '#475569';

  const usdt = (v: number | null | undefined) => `${money(v) ?? '—'} USDT`;

  // Group monthly returns by year
  const monthlyByYear: Record<number, Record<number, number>> = {};
  if (monthlyReturns) {
    for (const mr of monthlyReturns) {
      const [year, monthNum] = mr.month.split('-').map(Number);
      if (!monthlyByYear[year]) monthlyByYear[year] = {};
      monthlyByYear[year][monthNum] = mr.return_pct;
    }
  }
  const years = Object.keys(monthlyByYear).map(Number).sort();

  const strategyName = (id: string) => strategies.find((st) => st.id === id)?.name ?? (id || '—');

  const tradeColumns = [
    { key: 'timestamp', header: 'Time', render: (row: TradeRow) => utc(row.timestamp) ?? '—' },
    { key: 'symbol', header: 'Symbol' },
    { key: 'strategy_id', header: 'Strategy', hideOnMobile: true, render: (row: TradeRow) => strategyName(row.strategy_id) },
    {
      key: 'side',
      header: 'Side',
      render: (row: TradeRow) => {
        const side = sideLabel(row.side);
        return <span className={side.caution ? 'text-caution' : undefined}>{side.text}</span>;
      },
    },
    { key: 'price', header: 'Price', numeric: true, hideOnMobile: true, render: (row: TradeRow) => price(row.price) ?? '—' },
    { key: 'quantity', header: 'Qty', numeric: true, hideOnMobile: true, render: (row: TradeRow) => (row.quantity > 0 ? qty(row.quantity) : '—') },
    {
      key: 'pnl',
      header: 'PnL (USDT)',
      numeric: true,
      render: (row: TradeRow) => <SignedValue value={row.pnl} />,
    },
    {
      key: 'notes',
      header: 'Notes',
      render: (row: TradeRow) => (
        editingNoteId === row.id ? (
          <input
            aria-label="Note"
            className="h-8 w-32 rounded border border-line bg-canvas px-2 text-xs text-ink-1 focus:outline-none focus:ring-1 focus:ring-accent"
            value={editingNoteValue}
            onChange={(e) => setEditingNoteValue(e.target.value)}
            onBlur={() => handleNoteSave(row.id)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleNoteSave(row.id); if (e.key === 'Escape') setEditingNoteId(null); }}
            autoFocus
          />
        ) : (
          <button
            className="min-h-8 min-w-[60px] cursor-pointer text-left text-xs text-ink-2 hover:text-ink-1"
            onClick={() => { setEditingNoteId(row.id); setEditingNoteValue(row.notes); }}
          >
            {row.notes || 'Add note'}
          </button>
        )
      ),
    },
    {
      key: 'tags',
      header: 'Tags',
      render: (row: TradeRow) => (
        <div className="flex flex-wrap items-center gap-1">
          {row.tags.map((tag, i) => (
            <span key={i} className="inline-flex items-center gap-0.5 rounded-full bg-raised px-2 py-0.5 text-xs text-ink-1">
              {tag}
              <button aria-label={`Remove tag ${tag}`} onClick={() => handleRemoveTag(row.id, i)} className="hover:text-danger">
                <X className="h-3 w-3" aria-hidden="true" />
              </button>
            </span>
          ))}
          {addingTagId === row.id ? (
            <input
              aria-label="New tag"
              className="h-8 w-20 rounded border border-line bg-canvas px-1.5 text-xs text-ink-1 focus:outline-none focus:ring-1 focus:ring-accent"
              value={newTagValue}
              onChange={(e) => setNewTagValue(e.target.value)}
              onBlur={() => { if (newTagValue.trim()) handleAddTag(row.id); else setAddingTagId(null); }}
              onKeyDown={(e) => { if (e.key === 'Enter') handleAddTag(row.id); if (e.key === 'Escape') { setAddingTagId(null); setNewTagValue(''); } }}
              autoFocus
              placeholder="tag"
            />
          ) : (
            <button
              aria-label="Add tag"
              className="inline-flex h-6 w-6 items-center justify-center rounded-full border border-dashed border-line-strong text-ink-2 hover:text-ink-1"
              onClick={() => { setAddingTagId(row.id); setNewTagValue(''); }}
            >
              <Plus className="h-3 w-3" aria-hidden="true" />
            </button>
          )}
        </div>
      ),
    },
  ];

  const positionColumns = [
    { key: 'pair', header: 'Pair' },
    {
      key: 'side',
      header: 'Side',
      render: (row: Position) => {
        const side = sideLabel(row.side);
        return <span className={side.caution ? 'text-caution' : undefined}>{side.text}</span>;
      },
    },
    { key: 'size', header: 'Size', numeric: true, render: (row: Position) => qty(row.size) ?? '—' },
    { key: 'entry_price', header: 'Entry', numeric: true, hideOnMobile: true, render: (row: Position) => price(row.entry_price) ?? '—' },
    { key: 'current_price', header: 'Last stored close', numeric: true, render: (row: Position) => price(row.current_price) ?? '—' },
    { key: 'unrealized_pnl', header: 'Unrealised (USDT)', numeric: true, render: (row: Position) => <SignedValue value={row.unrealized_pnl} /> },
  ];

  const tradeRows: TradeRow[] = trades;

  const summaryNode = fetchErrors.summary ? (
    <DataStateErrorCard error={fetchErrors.summary} fallback="Failed to load portfolio summary" onRetry={loadData} />
  ) : loading ? (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="h-24 animate-pulse rounded-lg border border-line bg-raised" />
      ))}
    </div>
  ) : summary ? (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <Readout label="Total value" value={usdt(summary.total_value)} />
      <Readout label="Unrealised PnL" value={<SignedValue value={summary.unrealized_pnl} />} note="USDT" />
      <Readout label="Realised PnL" value={<SignedValue value={summary.realized_pnl} />} note="USDT" />
      <Readout label="Total fees" value={usdt(summary.total_fees)} />
    </div>
  ) : (
    <p className="py-8 text-center text-sm text-ink-2">NO DATA</p>
  );

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-semibold text-ink-1">Performance</h1>
      {storedMode !== null && storedMode !== 'paper' && (
        <p className="text-xs text-ink-2" data-testid="stored-mode-note">
          Stored mode is {storedMode}; live trading is refused until a later release. Showing paper.
        </p>
      )}

      {summary?.paper_capital_pending != null && (
        <p className="rounded-lg border border-caution/30 bg-caution-soft p-3 text-xs text-ink-1" data-testid="capital-pending">
          Paper capital change to {summary.paper_capital_pending.toLocaleString('en-US')} USDT is pending: the
          ledger rebases once every held position has a mark. Values below use the capital in force.
        </p>
      )}
      {summaryNode}

      <Section title="Export trades">
        <div className="flex flex-wrap items-end gap-3">
          <Select
            label="Format"
            options={[
              { value: 'generic', label: 'Generic CSV' },
              { value: 'koinly', label: 'Koinly' },
              { value: 'turbotax', label: 'TurboTax' },
            ]}
            value={exportFormat}
            onChange={(e) => setExportFormat(e.target.value)}
            className="w-40 max-lg:w-full"
          />
          <Input label="From" type="date" value={exportFromDate} onChange={(e) => setExportFromDate(e.target.value)} className="w-40 max-lg:w-full" />
          <Input label="To" type="date" value={exportToDate} onChange={(e) => setExportToDate(e.target.value)} className="w-40 max-lg:w-full" />
          <Button variant="primary" size="md" onClick={handleExport} loading={exporting}>
            <Download className="h-4 w-4" aria-hidden="true" /> Export
          </Button>
        </div>
      </Section>

      <Section
        title="Positions"
        action={<span className="text-xs text-ink-2">Mark time not reported</span>}
      >
        {fetchErrors.positions ? (
          <DataStateErrorCard error={fetchErrors.positions} fallback="Failed to load positions" onRetry={loadData} />
        ) : (
          <Table
            columns={positionColumns}
            data={positions}
            keyExtractor={(row) => row.id}
            loading={loading}
            emptyMessage="No open positions."
          />
        )}
      </Section>

      <Section title="Trade journal">
        <div className="mb-4 flex flex-wrap items-end gap-3">
          <Select
            label="Strategy"
            placeholder="All strategies"
            options={strategies.map((st) => ({ value: st.id, label: st.name }))}
            value={filterStrategy}
            onChange={(e) => setFilterStrategy(e.target.value)}
            className="w-44 max-lg:w-full"
          />
          <Input label="Symbol" placeholder="e.g. BTCUSDT" value={filterSymbol} onChange={(e) => setFilterSymbol(e.target.value)} className="w-36 max-lg:w-full" />
          <Input label="From" type="date" value={filterFromDate} onChange={(e) => setFilterFromDate(e.target.value)} className="w-40 max-lg:w-full" />
          <Input label="To" type="date" value={filterToDate} onChange={(e) => setFilterToDate(e.target.value)} className="w-40 max-lg:w-full" />
          <Input label="Min PnL" type="number" placeholder="0" value={filterMinPnl} onChange={(e) => setFilterMinPnl(e.target.value)} className="w-28 max-lg:w-full" />
          <Input label="Max PnL" type="number" placeholder="0" value={filterMaxPnl} onChange={(e) => setFilterMaxPnl(e.target.value)} className="w-28 max-lg:w-full" />
          <Button variant="primary" size="md" onClick={handleApplyFilters}>
            <Search className="h-4 w-4" aria-hidden="true" /> Apply filters
          </Button>
        </div>

        <Table
          columns={tradeColumns}
          data={tradeRows}
          keyExtractor={(row) => row.id}
          loading={tradesLoading}
          emptyMessage="No trades found. Apply filters and click Apply."
        />

        {tradesPages > 1 && (
          <div className="mt-4 flex items-center justify-between border-t border-line pt-3">
            <p className="text-xs text-ink-2">
              Page {tradesPage} of {tradesPages} ({tradesTotal} trades)
            </p>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" disabled={tradesPage <= 1} onClick={() => loadTrades(tradesPage - 1)}>
                Prev
              </Button>
              <Button variant="outline" size="sm" disabled={tradesPage >= tradesPages} onClick={() => loadTrades(tradesPage + 1)}>
                Next
              </Button>
            </div>
          </div>
        )}
      </Section>

      {/* Charts row */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Equity Curve */}
        <Section title="Equity Curve">
          {fetchErrors.equity ? (
            <DataStateErrorCard error={fetchErrors.equity} fallback="Failed to load equity curve" onRetry={loadData} />
          ) : loading ? (
            <div className="h-64 animate-pulse rounded-lg bg-raised" />
          ) : equityCurve && equityCurve.length > 0 ? (
            <div className="h-64" role="img" aria-label={`Equity curve, ${equityCurve.length} points, last value ${usdt(equityCurve[equityCurve.length - 1].value)}`}>
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={equityCurve} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="portfolioEquityGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={accentColor} stopOpacity={0.2} />
                      <stop offset="95%" stopColor={accentColor} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={gridColor} vertical={false} />
                  <XAxis
                    dataKey="timestamp"
                    tickFormatter={formatXTick}
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
                  <Tooltip
                    contentStyle={{
                      background: isDark ? '#1e2433' : '#ffffff',
                      border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : '#e2e8f0'}`,
                      borderRadius: '8px',
                      fontSize: '12px',
                    }}
                    formatter={(raw) => { const v = Number(raw); return [`$${v.toLocaleString('en-US', { minimumFractionDigits: 2 })}`, 'Value']; }}
                    labelFormatter={(l) => formatXTick(String(l))}
                  />
                  <Area
                    type="monotone"
                    dataKey="value"
                    stroke={accentColor}
                    strokeWidth={2}
                    fill="url(#portfolioEquityGradient)"
                    dot={false}
                    activeDot={{ r: 4, fill: accentColor, strokeWidth: 0 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-line bg-surface">
              <p className="text-sm text-ink-2">No data yet</p>
            </div>
          )}
        </Section>

        {/* Drawdown Chart */}
        <Section title="Drawdown">
          {fetchErrors.equity ? (
            <DataStateErrorCard error={fetchErrors.equity} fallback="Failed to load drawdown data" onRetry={loadData} />
          ) : loading ? (
            <div className="h-64 animate-pulse rounded-lg bg-raised" />
          ) : equityCurve && equityCurve.length > 0 ? (
            <div className="h-64" role="img" aria-label={`Drawdown, worst ${Math.max(...drawdownData.map((d) => d.drawdown)).toFixed(2)}%`}>
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={drawdownData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="portfolioDrawdownGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#ef4444" stopOpacity={0.2} />
                      <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={gridColor} vertical={false} />
                  <XAxis
                    dataKey="timestamp"
                    tickFormatter={formatXTick}
                    tick={{ fontSize: 11, fill: textMuted }}
                    axisLine={false}
                    tickLine={false}
                    minTickGap={40}
                  />
                  <YAxis
                    tickFormatter={formatDrawdownTick}
                    tick={{ fontSize: 11, fill: textMuted }}
                    axisLine={false}
                    tickLine={false}
                    width={44}
                    reversed
                  />
                  <Tooltip
                    contentStyle={{
                      background: isDark ? '#1e2433' : '#ffffff',
                      border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : '#e2e8f0'}`,
                      borderRadius: '8px',
                      fontSize: '12px',
                    }}
                    formatter={(raw) => { const v = Number(raw); return [`${v.toFixed(2)}%`, 'Drawdown']; }}
                    labelFormatter={(l) => formatXTick(String(l))}
                  />
                  <Area
                    type="monotone"
                    dataKey="drawdown"
                    stroke="#ef4444"
                    strokeWidth={2}
                    fill="url(#portfolioDrawdownGradient)"
                    dot={false}
                    activeDot={{ r: 4, fill: '#ef4444', strokeWidth: 0 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-line bg-surface">
              <p className="text-sm text-ink-2">No data yet</p>
            </div>
          )}
        </Section>
      </div>

      {equityCurve && equityCurve.length > 0 && (
        <Section title="Chart data">
          <details>
            <summary className="cursor-pointer text-sm text-ink-1">Equity and daily P&amp;L as tables</summary>
            <div className="mt-3 grid max-h-72 grid-cols-1 gap-4 overflow-y-auto lg:grid-cols-2">
              <table className="w-full text-xs" data-testid="equity-table">
                <caption className="pb-1 text-left font-semibold text-ink-1">Equity (USDT)</caption>
                <thead>
                  <tr>
                    <th scope="col" className="text-left text-ink-2">Time</th>
                    <th scope="col" className="text-right text-ink-2">Value</th>
                  </tr>
                </thead>
                <tbody>
                  {equityCurve.map((p, i) => (
                    <tr key={i}>
                      <td className="py-0.5 text-ink-1">{formatXTick(p.timestamp)}</td>
                      <td className="py-0.5 text-right text-ink-1">{usdt(p.value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <table className="w-full text-xs" data-testid="daily-pnl-table">
                <caption className="pb-1 text-left font-semibold text-ink-1">Daily P&amp;L (USDT)</caption>
                <thead>
                  <tr>
                    <th scope="col" className="text-left text-ink-2">Date</th>
                    <th scope="col" className="text-right text-ink-2">P&amp;L</th>
                  </tr>
                </thead>
                <tbody>
                  {dailyPnl.map((d, i) => (
                    <tr key={i}>
                      <td className="py-0.5 text-ink-1">{d.date}</td>
                      <td className="py-0.5 text-right"><SignedValue value={d.pnl} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </Section>
      )}

      {/* Monthly returns heatmap */}
      <Section title="Monthly Returns">
        {fetchErrors.monthlyReturns ? (
          <DataStateErrorCard error={fetchErrors.monthlyReturns} fallback="Failed to load monthly returns" onRetry={loadData} />
        ) : loading ? (
          <div className="h-32 animate-pulse rounded-lg bg-raised" />
        ) : monthlyReturns && monthlyReturns.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr>
                  <th className="px-2 py-1 text-left text-ink-2 font-semibold">Year</th>
                  {MONTH_LABELS.map((m) => (
                    <th key={m} className="px-1 py-1 text-center text-ink-2 font-medium">{m}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {years.map((year) => (
                  <tr key={year}>
                    <td className="px-2 py-1 text-ink-1 font-medium">{year}</td>
                    {Array.from({ length: 12 }).map((_, mi) => {
                      const val = monthlyByYear[year]?.[mi + 1];
                      return (
                        <td key={mi} className="px-1 py-1">
                          {val !== undefined ? (
                            <div
                              className={`rounded px-1.5 py-1 text-center text-xs font-medium ${getReturnColor(val)}`}
                              title={`${MONTH_LABELS[mi]} ${year}: ${val >= 0 ? '+' : ''}${val.toFixed(1)}%`}
                            >
                              {val >= 0 ? '+' : ''}{val.toFixed(1)}%
                            </div>
                          ) : (
                            <div className="rounded px-1.5 py-1 text-center text-xs text-ink-2 bg-surface">
                              -
                            </div>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-ink-2 text-center py-8">No data yet</p>
        )}
      </Section>

      {/* Attribution bar chart */}
      <Section title="Strategy Attribution">
        {fetchErrors.attribution ? (
          <DataStateErrorCard error={fetchErrors.attribution} fallback="Failed to load attribution data" onRetry={loadData} />
        ) : loading ? (
          <div className="h-56 animate-pulse rounded-lg bg-raised" />
        ) : attribution && attribution.length > 0 ? (
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={attribution}
                layout="vertical"
                margin={{ top: 4, right: 4, left: 0, bottom: 0 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} horizontal={false} />
                <XAxis
                  type="number"
                  tickFormatter={(v: number) => `$${v}`}
                  tick={{ fontSize: 11, fill: textMuted }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  type="category"
                  dataKey="strategy"
                  tick={{ fontSize: 11, fill: textMuted }}
                  axisLine={false}
                  tickLine={false}
                  width={120}
                />
                <Tooltip
                  contentStyle={{
                    background: isDark ? '#1e2433' : '#ffffff',
                    border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : '#e2e8f0'}`,
                    borderRadius: '8px',
                    fontSize: '12px',
                  }}
                  formatter={(raw) => { const v = Number(raw); return [`$${v.toFixed(2)}`, 'PnL']; }}
                />
                <Bar dataKey="pnl" radius={[0, 3, 3, 0]}>
                  {attribution.map((entry, index) => (
                    <Cell
                      key={`attr-${index}`}
                      fill={entry.pnl >= 0 ? '#22c55e' : '#ef4444'}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-sm text-ink-2 text-center py-8">No data yet</p>
        )}
      </Section>

      {/* Allocation breakdown */}
      <Section title="Position Allocation">
        {fetchErrors.positions ? (
          <DataStateErrorCard error={fetchErrors.positions} fallback="Failed to load positions" onRetry={loadData} />
        ) : loading ? (
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-8 animate-pulse rounded-lg bg-raised" />
            ))}
          </div>
        ) : allocationItems.length > 0 ? (
          <div className="space-y-3">
            {allocationItems.map((item) => (
              <div key={item.label}>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm text-ink-1">{item.label}</span>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-ink-1">{item.value}</span>
                    <span className="text-xs text-ink-2">{item.allocation}%</span>
                  </div>
                </div>
                <div className="h-2 rounded-full bg-raised overflow-hidden">
                  <div
                    className="h-full rounded-full bg-accent transition-all"
                    style={{ width: `${item.allocation}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-ink-2 text-center py-8">No data yet</p>
        )}
      </Section>
    </div>
  );
}
