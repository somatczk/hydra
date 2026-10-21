'use client';

import { useEffect, useState, useMemo } from 'react';
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
import { Select } from '@/components/ui/Select';
import { DataCard } from '@/components/ui/DataCard';
import { fetchApi } from '@/lib/api';
import { logger } from '@/lib/logger';
import { ResearchNav } from '@/components/research/ResearchNav';
import {
  type TimeResolution,
  type EquityPoint,
  aggregateEquityCurve,
  formatXTick,
  formatYTick,
} from '../chart-utils';
import { Button } from '@/components/ui/Button';
import { DataStateErrorCard } from '@/components/ui/DataStateBadge';
import {
  ProvenanceBadge,
  provenanceMismatch,
  type DataProvenance,
} from '@/components/backtest/DataProvenance';

/* ---------- Types ---------- */

interface BacktestListItem {
  id: string;
  strategy: string;
  name: string;
  period: string;
  status: string;
  provenance?: DataProvenance | null;
}

interface BacktestDetail {
  id: string;
  strategy: string;
  period: string;
  name: string;
  provenance?: DataProvenance | null;
  metrics: {
    total_trades: number;
    win_rate: number;
    total_pnl: number;
    max_drawdown: number;
    sharpe_ratio: number;
  };
  equity_curve: EquityPoint[];
}

interface MergedPoint {
  timestamp: string;
  valueA?: number;
  valueB?: number;
}

/* ---------- Helpers ---------- */

function mergeEquityCurves(
  curveA: EquityPoint[],
  curveB: EquityPoint[],
  resolution: TimeResolution,
): MergedPoint[] {
  const aggA = aggregateEquityCurve(curveA, resolution);
  const aggB = aggregateEquityCurve(curveB, resolution);

  const map = new Map<string, MergedPoint>();

  for (const p of aggA) {
    map.set(p.timestamp, { timestamp: p.timestamp, valueA: p.value });
  }
  for (const p of aggB) {
    const existing = map.get(p.timestamp);
    if (existing) {
      existing.valueB = p.value;
    } else {
      map.set(p.timestamp, { timestamp: p.timestamp, valueB: p.value });
    }
  }

  return Array.from(map.values()).sort(
    (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime(),
  );
}

function fmtPnl(v: number): string {
  const sign = v >= 0 ? '+' : '-';
  return `${sign}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
}

/* ---------- Page ---------- */

export default function BacktestComparePage() {
  const [results, setResults] = useState<BacktestListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<unknown>(null);
  const [detailError, setDetailError] = useState<unknown>(null);
  const [selectedA, setSelectedA] = useState('');
  const [selectedB, setSelectedB] = useState('');
  const [detailA, setDetailA] = useState<BacktestDetail | null>(null);
  const [detailB, setDetailB] = useState<BacktestDetail | null>(null);
  const [loadingDetails, setLoadingDetails] = useState(false);
  const [resolution, setResolution] = useState<TimeResolution>('days');
  const [isDark, setIsDark] = useState(false);

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains('dark'));
  }, []);

  const loadResults = () => {
    setListError(null);
    fetchApi<BacktestListItem[]>('/api/backtest/results')
      .then((data) => {
        const completed = data.filter((r) => r.status === 'completed');
        setResults(completed);
      })
      .catch((err) => {
        logger.warn('BacktestCompare', 'Failed to fetch results list', err);
        setListError(err);
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => { loadResults(); }, []);

  useEffect(() => {
    if (!selectedA || !selectedB) {
      setDetailA(null);
      setDetailB(null);
      return;
    }
    setLoadingDetails(true);
    setDetailError(null);
    Promise.all([
      fetchApi<BacktestDetail>(`/api/backtest/results/${selectedA}`),
      fetchApi<BacktestDetail>(`/api/backtest/results/${selectedB}`),
    ])
      .then(([a, b]) => {
        setDetailA(a);
        setDetailB(b);
      })
      .catch((err) => {
        logger.error('BacktestCompare', 'Failed to fetch backtest details', err);
        setDetailError(err);
      })
      .finally(() => setLoadingDetails(false));
  }, [selectedA, selectedB]);

  const accentA = isDark ? '#2383e2' : '#2563eb';
  const accentB = isDark ? '#e88c30' : '#ea580c';
  const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)';
  const textMuted = isDark ? 'rgba(255,255,255,0.44)' : '#475569';

  const chartData = useMemo(() => {
    if (!detailA || !detailB) return [];
    return mergeEquityCurves(detailA.equity_curve, detailB.equity_curve, resolution);
  }, [detailA, detailB, resolution]);

  const mismatch = detailA && detailB ? provenanceMismatch(detailA.provenance, detailB.provenance) : [];
  const selectedProvenance = (id: string) => results.find((r) => r.id === id)?.provenance;

  const resultOptions = results.map((r) => ({
    value: r.id,
    label: r.name || `${r.strategy} (${r.period})`,
  }));

  const comparisonMetrics = useMemo(() => {
    if (!detailA || !detailB) return null;
    const mA = detailA.metrics;
    const mB = detailB.metrics;
    return [
      { label: 'Total Trades', a: String(mA.total_trades), b: String(mB.total_trades) },
      { label: 'Win Rate', a: `${mA.win_rate.toFixed(1)}%`, b: `${mB.win_rate.toFixed(1)}%` },
      { label: 'Total PnL', a: fmtPnl(mA.total_pnl), b: fmtPnl(mB.total_pnl), colorA: mA.total_pnl >= 0, colorB: mB.total_pnl >= 0 },
      { label: 'Max Drawdown', a: `${mA.max_drawdown.toFixed(1)}%`, b: `${mB.max_drawdown.toFixed(1)}%` },
      { label: 'Sharpe Ratio', a: mA.sharpe_ratio.toFixed(2), b: mB.sharpe_ratio.toFixed(2) },
    ];
  }, [detailA, detailB]);

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-ink-2">Loading backtest results...</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <ResearchNav current="/backtest" />
      <div>
        <h1 className="text-xl font-semibold text-ink-1">Backtest comparison</h1>
        <p className="text-sm text-ink-2 mt-1">
          Compare two backtest results side by side
        </p>
      </div>

      {/* Selectors */}
      <DataCard>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Select
            label="Backtest A"
            options={resultOptions}
            value={selectedA}
            onChange={(e) => setSelectedA(e.target.value)}
            placeholder="Select backtest..."
          />
          <Select
            label="Backtest B"
            options={resultOptions}
            value={selectedB}
            onChange={(e) => setSelectedB(e.target.value)}
            placeholder="Select backtest..."
          />
        </div>
        {(selectedA || selectedB) && (
          <div className="mt-3 grid grid-cols-1 gap-4 text-xs text-ink-2 md:grid-cols-2">
            <div className="flex items-center gap-2" data-testid="data-a">
              {selectedA && <>Data: <ProvenanceBadge provenance={selectedProvenance(selectedA)} /></>}
            </div>
            <div className="flex items-center gap-2" data-testid="data-b">
              {selectedB && <>Data: <ProvenanceBadge provenance={selectedProvenance(selectedB)} /></>}
            </div>
          </div>
        )}
      </DataCard>

      {listError !== null && (
        <DataStateErrorCard error={listError} fallback="Failed to load backtest results" onRetry={loadResults} />
      )}

      {detailError !== null && (
        <DataStateErrorCard error={detailError} fallback="Failed to load the selected backtests" />
      )}

      {mismatch.length > 0 && (
        <div className="rounded-xl border border-status-warning/30 bg-caution/10 px-4 py-3" data-testid="provenance-mismatch">
          <p className="text-sm font-medium text-ink-1">These runs used different data</p>
          <p className="text-xs text-ink-2">{mismatch.join('; ')}</p>
        </div>
      )}

      {loadingDetails && (
        <div className="flex h-32 items-center justify-center">
          <p className="text-sm text-ink-2">Loading comparison data...</p>
        </div>
      )}

      {/* Comparison table */}
      {comparisonMetrics && detailA && detailB && (
        <>
          <DataCard title="Metrics Comparison">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line">
                    <th className="pb-2 text-left text-xs font-medium text-ink-2">Metric</th>
                    <th className="pb-2 text-right text-xs font-medium text-ink-2">
                      {detailA.name || detailA.strategy}
                    </th>
                    <th className="pb-2 text-right text-xs font-medium text-ink-2">
                      {detailB.name || detailB.strategy}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {comparisonMetrics.map((row) => (
                    <tr key={row.label} className="border-b border-line last:border-0">
                      <td className="py-2.5 text-ink-2">{row.label}</td>
                      <td className={`py-2.5 text-right font-medium ${'colorA' in row ? (row.colorA ? 'text-ink-1' : 'text-danger') : 'text-ink-1'}`}>
                        {row.a}
                      </td>
                      <td className={`py-2.5 text-right font-medium ${'colorB' in row ? (row.colorB ? 'text-ink-1' : 'text-danger') : 'text-ink-1'}`}>
                        {row.b}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataCard>

          {/* Overlaid equity curves */}
          <DataCard title="Equity Curves">
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
            <div className="h-72">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="compareGradientA" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={accentA} stopOpacity={0.15} />
                      <stop offset="95%" stopColor={accentA} stopOpacity={0} />
                    </linearGradient>
                    <linearGradient id="compareGradientB" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={accentB} stopOpacity={0.15} />
                      <stop offset="95%" stopColor={accentB} stopOpacity={0} />
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
                  <Tooltip
                    content={({ active, payload, label }) => {
                      if (!active || !payload?.length) return null;
                      return (
                        <div className="rounded-lg border border-line bg-surface px-3 py-2 shadow-lg">
                          <p className="text-xs text-ink-2">{label != null ? formatXTick(String(label), resolution) : ''}</p>
                          {payload.map((p) => (
                            <p key={String(p.dataKey)} className="text-sm font-medium text-ink-1">
                              {p.dataKey === 'valueA' ? 'A' : 'B'}: ${(p.value as number)?.toLocaleString('en-US', { minimumFractionDigits: 2 }) ?? 'N/A'}
                            </p>
                          ))}
                        </div>
                      );
                    }}
                  />
                  <Legend
                    formatter={(value) => (
                      <span className="text-xs text-ink-2">
                        {value === 'valueA' ? (detailA.name || detailA.strategy) : (detailB.name || detailB.strategy)}
                      </span>
                    )}
                  />
                  <Area
                    type="monotone"
                    dataKey="valueA"
                    stroke={accentA}
                    strokeWidth={2}
                    fill="url(#compareGradientA)"
                    dot={false}
                    activeDot={{ r: 4, fill: accentA, strokeWidth: 0 }}
                  />
                  <Area
                    type="monotone"
                    dataKey="valueB"
                    stroke={accentB}
                    strokeWidth={2}
                    fill="url(#compareGradientB)"
                    dot={false}
                    activeDot={{ r: 4, fill: accentB, strokeWidth: 0 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </DataCard>
        </>
      )}

      {!selectedA && !selectedB && results.length > 0 && (
        <div className="flex flex-col items-center justify-center py-12">
          <p className="text-sm text-ink-2">Select two backtest results above to compare</p>
        </div>
      )}

      {listError === null && results.length < 2 && (
        <div className="flex flex-col items-center justify-center py-12">
          <p className="text-sm text-ink-2">At least two completed backtests are needed for comparison</p>
        </div>
      )}
    </div>
  );
}
