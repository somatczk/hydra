'use client';

import { useEffect, useState, useCallback } from 'react';
import {
  Wallet,
  TrendingUp,
  TrendingDown,
  Receipt,
  Download,
  Search,
  Plus,
  X,
} from 'lucide-react';
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
import { StatCard } from '@/components/ui/DataCard';
import { DataCard } from '@/components/ui/DataCard';
import { ErrorCard } from '@/components/ui/ErrorCard';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Table } from '@/components/ui/Table';
import { useToast } from '@/components/ui/Toast';
import { fetchApi } from '@/lib/api';
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
  strategy: string;
  symbol: string;
  side: string;
  entry_time: string;
  exit_time: string;
  entry_price: number;
  exit_price: number;
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

interface SentimentData {
  value: number;
  classification: string;
  timestamp: string;
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

function getReturnColor(pct: number): string {
  if (pct > 5) return 'bg-status-success';
  if (pct > 2) return 'bg-status-success/70';
  if (pct > 0) return 'bg-status-success/40';
  if (pct === 0) return 'bg-bg-tertiary';
  if (pct > -2) return 'bg-status-error/40';
  if (pct > -5) return 'bg-status-error/70';
  return 'bg-status-error';
}

function getSentimentLabel(value: number): string {
  if (value <= 24) return 'Extreme Fear';
  if (value <= 44) return 'Fear';
  if (value <= 55) return 'Neutral';
  if (value <= 74) return 'Greed';
  return 'Extreme Greed';
}

function getSentimentColor(value: number): string {
  if (value <= 24) return '#ef4444';
  if (value <= 44) return '#f97316';
  if (value <= 55) return '#eab308';
  if (value <= 74) return '#84cc16';
  return '#22c55e';
}

/* ---------- Trade Table Row ---------- */

interface TradeRow {
  id: string;
  strategy: string;
  symbol: string;
  side: string;
  entry_time: string;
  exit_time: string;
  pnl: number;
  pnlFormatted: string;
  notes: string;
  tags: string[];
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
  const [isDark, setIsDark] = useState(false);
  const [fetchErrors, setFetchErrors] = useState<Record<string, boolean>>({});

  // Sentiment state
  const [sentiment, setSentiment] = useState<SentimentData | null>(null);

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

    const cfg = await fetchApi<{ trading_mode: string }>('/api/system/config')
      .catch(() => ({ trading_mode: 'paper' }));
    const source = cfg.trading_mode === 'live' ? 'live' : 'paper';
    const qs = `?source=${source}`;

    await Promise.all([
      fetchApi<PortfolioSummary>(`/api/portfolio/summary${qs}`)
        .then(setSummary)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, summary: true })); }),
      fetchApi<Position[]>(`/api/portfolio/positions${qs}`)
        .then(setPositions)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, positions: true })); }),
      fetchApi<EquityPoint[]>(`/api/portfolio/equity-curve${qs}`)
        .then(setEquityCurve)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, equity: true })); }),
      fetchApi<DailyPnl[]>(`/api/portfolio/daily-pnl${qs}`)
        .then(setDailyPnl)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, dailyPnl: true })); }),
      fetchApi<MonthlyReturn[]>(`/api/portfolio/monthly-returns${qs}`)
        .then(setMonthlyReturns)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, monthlyReturns: true })); }),
      fetchApi<AttributionItem[]>(`/api/portfolio/attribution${qs}`)
        .then(setAttribution)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, attribution: true })); }),
      fetchApi<SentimentData[]>('/api/market/sentiment')
        .then((arr) => setSentiment(arr?.[0] ?? null))
        .catch(() => { setFetchErrors((prev) => ({ ...prev, sentiment: true })); }),
      fetchApi<StrategyOption[]>('/api/strategies')
        .then((data) => setStrategies(data.map((s) => ({ id: s.id, name: s.name }))))
        .catch(() => {}),
    ]);

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
    } catch {
      toast('error', 'Failed to load trades');
    } finally {
      setTradesLoading(false);
    }
  }, [filterStrategy, filterSymbol, filterFromDate, filterToDate, filterMinPnl, filterMaxPnl, toast]);

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
    if (exportFromDate) params.set('from_date', exportFromDate);
    if (exportToDate) params.set('to_date', exportToDate);
    try {
      const url = `${process.env.NEXT_PUBLIC_API_URL || window.location.origin}/api/portfolio/export/csv?${params.toString()}`;
      const res = await fetch(url);
      if (!res.ok) throw new Error('Export failed');
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `trades_${exportFormat}_${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
      toast('success', 'Export downloaded');
    } catch {
      toast('error', 'Failed to export trades');
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

  const fmt = (v: number) => `$${v.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;

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

  // Trade table columns
  const tradeColumns = [
    { key: 'symbol', header: 'Symbol' },
    { key: 'strategy', header: 'Strategy', hideOnMobile: true },
    { key: 'side', header: 'Side' },
    { key: 'entry_time', header: 'Entry', render: (row: TradeRow) => new Date(row.entry_time).toLocaleDateString() },
    { key: 'exit_time', header: 'Exit', hideOnMobile: true, render: (row: TradeRow) => new Date(row.exit_time).toLocaleDateString() },
    {
      key: 'pnl',
      header: 'PnL',
      render: (row: TradeRow) => (
        <span className={row.pnl >= 0 ? 'text-status-success font-medium' : 'text-status-error font-medium'}>
          {row.pnlFormatted}
        </span>
      ),
    },
    {
      key: 'notes',
      header: 'Notes',
      render: (row: TradeRow) => (
        editingNoteId === row.id ? (
          <input
            className="h-7 w-32 rounded border border-border-default bg-bg-primary px-2 text-xs text-text-primary focus:outline-none focus:ring-1 focus:ring-border-focus"
            value={editingNoteValue}
            onChange={(e) => setEditingNoteValue(e.target.value)}
            onBlur={() => handleNoteSave(row.id)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleNoteSave(row.id); if (e.key === 'Escape') setEditingNoteId(null); }}
            autoFocus
          />
        ) : (
          <button
            className="text-xs text-text-muted hover:text-text-primary cursor-pointer min-w-[60px] text-left"
            onClick={() => { setEditingNoteId(row.id); setEditingNoteValue(row.notes); }}
          >
            {row.notes || 'Add note...'}
          </button>
        )
      ),
    },
    {
      key: 'tags',
      header: 'Tags',
      render: (row: TradeRow) => (
        <div className="flex items-center gap-1 flex-wrap">
          {row.tags.map((tag, i) => (
            <span key={i} className="inline-flex items-center gap-0.5 rounded-full bg-accent-primary/10 px-2 py-0.5 text-xs text-accent-primary">
              {tag}
              <button onClick={() => handleRemoveTag(row.id, i)} className="hover:text-status-error">
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
          {addingTagId === row.id ? (
            <input
              className="h-6 w-20 rounded border border-border-default bg-bg-primary px-1.5 text-xs text-text-primary focus:outline-none focus:ring-1 focus:ring-border-focus"
              value={newTagValue}
              onChange={(e) => setNewTagValue(e.target.value)}
              onBlur={() => { if (newTagValue.trim()) handleAddTag(row.id); else setAddingTagId(null); }}
              onKeyDown={(e) => { if (e.key === 'Enter') handleAddTag(row.id); if (e.key === 'Escape') { setAddingTagId(null); setNewTagValue(''); } }}
              autoFocus
              placeholder="tag"
            />
          ) : (
            <button
              className="inline-flex items-center justify-center h-5 w-5 rounded-full border border-dashed border-border-default text-text-muted hover:text-text-primary hover:border-border-hover"
              onClick={() => { setAddingTagId(row.id); setNewTagValue(''); }}
            >
              <Plus className="h-3 w-3" />
            </button>
          )}
        </div>
      ),
    },
  ];

  const tradeRows: TradeRow[] = trades.map((t) => ({
    id: t.id,
    strategy: t.strategy,
    symbol: t.symbol,
    side: t.side,
    entry_time: t.entry_time,
    exit_time: t.exit_time,
    pnl: t.pnl,
    pnlFormatted: `${t.pnl >= 0 ? '+' : '-'}$${Math.abs(t.pnl).toLocaleString('en-US', { minimumFractionDigits: 2 })}`,
    notes: t.notes,
    tags: t.tags,
  }));

  return (
    <div className="flex flex-col gap-6">
      {/* Export section */}
      <DataCard title="Export Trades" description="Download trade history for tax reporting">
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
            className="w-40"
          />
          <Input
            label="From"
            type="date"
            value={exportFromDate}
            onChange={(e) => setExportFromDate(e.target.value)}
            className="w-40"
          />
          <Input
            label="To"
            type="date"
            value={exportToDate}
            onChange={(e) => setExportToDate(e.target.value)}
            className="w-40"
          />
          <Button variant="primary" size="md" onClick={handleExport} loading={exporting}>
            <Download className="h-4 w-4" /> Export
          </Button>
        </div>
      </DataCard>

      {/* Stat cards + Sentiment */}
      {fetchErrors.summary ? (
        <ErrorCard message="Failed to load portfolio summary" onRetry={loadData} />
      ) : loading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-28 animate-pulse rounded-xl border border-border-default bg-bg-tertiary" />
          ))}
        </div>
      ) : summary ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5">
          <StatCard
            icon={Wallet}
            label="Total Value"
            value={fmt(summary.total_value)}
            change={summary.change_pct}
            changeType={summary.change_pct >= 0 ? 'increase' : 'decrease'}
          />
          <StatCard
            icon={TrendingUp}
            label="Unrealized PnL"
            value={`${summary.unrealized_pnl >= 0 ? '+' : ''}${fmt(summary.unrealized_pnl)}`}
            changeType={summary.unrealized_pnl >= 0 ? 'increase' : 'decrease'}
          />
          <StatCard
            icon={TrendingDown}
            label="Realized PnL"
            value={`${summary.realized_pnl >= 0 ? '+' : ''}${fmt(summary.realized_pnl)}`}
            changeType={summary.realized_pnl >= 0 ? 'increase' : 'decrease'}
          />
          <StatCard
            icon={Receipt}
            label="Total Fees"
            value={fmt(summary.total_fees)}
          />
          {/* Sentiment widget */}
          {!fetchErrors.sentiment && sentiment ? (
            <DataCard padding="sm" className="flex flex-col justify-between">
              <p className="text-xs font-medium text-text-muted">Market Sentiment</p>
              <div className="mt-2">
                <div className="relative h-3 w-full rounded-full overflow-hidden bg-gradient-to-r from-red-500 via-yellow-500 to-green-500">
                  <div
                    className="absolute top-0 h-full w-1 bg-white rounded-full shadow-md"
                    style={{ left: `${sentiment.value}%` }}
                  />
                </div>
                <div className="mt-1.5 flex items-center justify-between">
                  <span className="text-lg font-bold" style={{ color: getSentimentColor(sentiment.value) }}>
                    {sentiment.value}
                  </span>
                  <span className="text-xs font-medium" style={{ color: getSentimentColor(sentiment.value) }}>
                    {getSentimentLabel(sentiment.value)}
                  </span>
                </div>
              </div>
            </DataCard>
          ) : !fetchErrors.sentiment && loading ? (
            <div className="h-28 animate-pulse rounded-xl border border-border-default bg-bg-tertiary" />
          ) : null}
        </div>
      ) : (
        <p className="text-sm text-text-muted text-center py-8">No data yet</p>
      )}

      {/* Trade Journal */}
      <DataCard title="Trade Journal" description="Filter and review your trades">
        {/* Filter bar */}
        <div className="flex flex-wrap items-end gap-3 mb-4">
          <Select
            label="Strategy"
            placeholder="All strategies"
            options={strategies.map((s) => ({ value: s.id, label: s.name }))}
            value={filterStrategy}
            onChange={(e) => setFilterStrategy(e.target.value)}
            className="w-44"
          />
          <Input
            label="Symbol"
            placeholder="e.g. BTCUSDT"
            value={filterSymbol}
            onChange={(e) => setFilterSymbol(e.target.value)}
            className="w-36"
          />
          <Input
            label="From"
            type="date"
            value={filterFromDate}
            onChange={(e) => setFilterFromDate(e.target.value)}
            className="w-40"
          />
          <Input
            label="To"
            type="date"
            value={filterToDate}
            onChange={(e) => setFilterToDate(e.target.value)}
            className="w-40"
          />
          <Input
            label="Min PnL"
            type="number"
            placeholder="0"
            value={filterMinPnl}
            onChange={(e) => setFilterMinPnl(e.target.value)}
            className="w-28"
          />
          <Input
            label="Max PnL"
            type="number"
            placeholder="0"
            value={filterMaxPnl}
            onChange={(e) => setFilterMaxPnl(e.target.value)}
            className="w-28"
          />
          <Button variant="primary" size="md" onClick={handleApplyFilters}>
            <Search className="h-4 w-4" /> Apply Filters
          </Button>
        </div>

        {/* Trades table */}
        <Table
          columns={tradeColumns}
          data={tradeRows}
          keyExtractor={(row) => row.id}
          loading={tradesLoading}
          emptyMessage="No trades found. Apply filters and click Apply."
        />

        {/* Pagination */}
        {tradesPages > 1 && (
          <div className="flex items-center justify-between mt-4 pt-3 border-t border-border-default">
            <p className="text-xs text-text-muted">
              Page {tradesPage} of {tradesPages} ({tradesTotal} trades)
            </p>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={tradesPage <= 1}
                onClick={() => loadTrades(tradesPage - 1)}
              >
                Prev
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={tradesPage >= tradesPages}
                onClick={() => loadTrades(tradesPage + 1)}
              >
                Next
              </Button>
            </div>
          </div>
        )}
      </DataCard>

      {/* Charts row */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Equity Curve */}
        <DataCard title="Equity Curve" description="Portfolio value over time">
          {fetchErrors.equity ? (
            <ErrorCard message="Failed to load equity curve" onRetry={loadData} />
          ) : loading ? (
            <div className="h-64 animate-pulse rounded-lg bg-bg-tertiary" />
          ) : equityCurve && equityCurve.length > 0 ? (
            <div className="h-64">
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
                    formatter={(v: number) => [`$${v.toLocaleString('en-US', { minimumFractionDigits: 2 })}`, 'Value']}
                    labelFormatter={formatXTick}
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
            <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
              <p className="text-sm text-text-muted">No data yet</p>
            </div>
          )}
        </DataCard>

        {/* Drawdown Chart */}
        <DataCard title="Drawdown" description="Maximum drawdown over time">
          {fetchErrors.equity ? (
            <ErrorCard message="Failed to load drawdown data" onRetry={loadData} />
          ) : loading ? (
            <div className="h-64 animate-pulse rounded-lg bg-bg-tertiary" />
          ) : equityCurve && equityCurve.length > 0 ? (
            <div className="h-64">
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
                    formatter={(v: number) => [`${v.toFixed(2)}%`, 'Drawdown']}
                    labelFormatter={formatXTick}
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
            <div className="flex h-64 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
              <p className="text-sm text-text-muted">No data yet</p>
            </div>
          )}
        </DataCard>
      </div>

      {/* Monthly returns heatmap */}
      <DataCard title="Monthly Returns" description="Calendar heatmap of monthly performance">
        {fetchErrors.monthlyReturns ? (
          <ErrorCard message="Failed to load monthly returns" onRetry={loadData} />
        ) : loading ? (
          <div className="h-32 animate-pulse rounded-lg bg-bg-tertiary" />
        ) : monthlyReturns && monthlyReturns.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr>
                  <th className="px-2 py-1 text-left text-text-muted font-semibold">Year</th>
                  {MONTH_LABELS.map((m) => (
                    <th key={m} className="px-1 py-1 text-center text-text-muted font-medium">{m}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {years.map((year) => (
                  <tr key={year}>
                    <td className="px-2 py-1 text-text-primary font-medium">{year}</td>
                    {Array.from({ length: 12 }).map((_, mi) => {
                      const val = monthlyByYear[year]?.[mi + 1];
                      return (
                        <td key={mi} className="px-1 py-1">
                          {val !== undefined ? (
                            <div
                              className={`rounded px-1.5 py-1 text-center text-xs font-medium text-white ${getReturnColor(val)}`}
                              title={`${MONTH_LABELS[mi]} ${year}: ${val >= 0 ? '+' : ''}${val.toFixed(1)}%`}
                            >
                              {val >= 0 ? '+' : ''}{val.toFixed(1)}%
                            </div>
                          ) : (
                            <div className="rounded px-1.5 py-1 text-center text-xs text-text-light bg-bg-secondary">
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
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>

      {/* Attribution bar chart */}
      <DataCard title="Strategy Attribution" description="PnL contribution per strategy">
        {fetchErrors.attribution ? (
          <ErrorCard message="Failed to load attribution data" onRetry={loadData} />
        ) : loading ? (
          <div className="h-56 animate-pulse rounded-lg bg-bg-tertiary" />
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
                  formatter={(v: number) => [`$${v.toFixed(2)}`, 'PnL']}
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
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>

      {/* Allocation breakdown */}
      <DataCard title="Position Allocation" description="Current portfolio breakdown">
        {fetchErrors.positions ? (
          <ErrorCard message="Failed to load positions" onRetry={loadData} />
        ) : loading ? (
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-8 animate-pulse rounded-lg bg-bg-tertiary" />
            ))}
          </div>
        ) : allocationItems.length > 0 ? (
          <div className="space-y-3">
            {allocationItems.map((item) => (
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
        ) : (
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>
    </div>
  );
}
