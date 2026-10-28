'use client';

import { Suspense, useEffect, useRef, useState, useMemo } from 'react';
import { useSearchParams } from 'next/navigation';
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
import { Section } from '@/components/ui/Section';
import { fetchApi } from '@/lib/api';
import { money, signed, signedLabel } from '@/lib/format';
import { logger } from '@/lib/logger';
import { ResearchNav } from '@/components/research/ResearchNav';
import {
  type TimeResolution,
  type EquityPoint,
  aggregateEquityCurve,
  chartColors,
  formatXTick,
  formatYTick,
} from '../chart-utils';
import { Button } from '@/components/ui/Button';
import { DataStateErrorCard } from '@/components/ui/DataStateBadge';
import {
  ProvenanceBadge,
  provenanceMismatch,
  formatUtc,
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

const usdt = (v: number) => `${money(v) ?? '—'} USDT`;

/** The sign glyph is hidden from AT, which hear "gain/loss" instead (portfolio pattern). */
function SignedMoney({ value }: { value: number }) {
  return (
    <>
      <span aria-hidden="true">{signed(value) ?? '—'}</span>
      <span className="sr-only">{signedLabel(value) ?? 'not available'}</span>
    </>
  );
}

function CompareLoading() {
  return (
    <div className="flex h-64 items-center justify-center">
      <p className="text-sm text-ink-2">Loading backtest results...</p>
    </div>
  );
}

/* ---------- Page ---------- */

function BacktestCompareContent() {
  const searchParams = useSearchParams();
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

  // Seed ?a=&b= only once the completed list is known, so a dead id (or a=b) falls
  // back to the empty "Choose runs" state instead of a blank/self-comparing select.
  const seededFromUrl = useRef(false);
  useEffect(() => {
    if (seededFromUrl.current || results.length === 0) return;
    seededFromUrl.current = true;
    const ids = new Set(results.map((r) => r.id));
    const a = searchParams.get('a');
    const b = searchParams.get('b');
    if (a && ids.has(a)) setSelectedA(a);
    if (b && ids.has(b) && b !== a) setSelectedB(b);
  }, [results, searchParams]);

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
        // Stale details would keep the table, chart and mismatch banner below the error card.
        setDetailA(null);
        setDetailB(null);
      })
      .finally(() => setLoadingDetails(false));
  }, [selectedA, selectedB]);

  const colors = chartColors();

  const chartData = useMemo(() => {
    if (!detailA || !detailB) return [];
    return mergeEquityCurves(detailA.equity_curve, detailB.equity_curve, resolution);
  }, [detailA, detailB, resolution]);

  const chartSummary = useMemo(() => {
    if (!detailA || !detailB) return null;
    const a = detailA.equity_curve;
    const b = detailB.equity_curve;
    if (a.length === 0 || b.length === 0) return null;
    // first/last (curves are bar-ordered): a Math.min spread overflows the arg limit
    const firstA = a[0].timestamp;
    const lastA = a[a.length - 1].timestamp;
    const firstB = b[0].timestamp;
    const lastB = b[b.length - 1].timestamp;
    const start = formatUtc(Date.parse(firstA) <= Date.parse(firstB) ? firstA : firstB);
    const end = formatUtc(Date.parse(lastA) >= Date.parse(lastB) ? lastA : lastB);
    return `Equity from ${start} to ${end}: A from ${usdt(a[0].value)} to ${usdt(a[a.length - 1].value)}, B from ${usdt(b[0].value)} to ${usdt(b[b.length - 1].value)}`;
  }, [detailA, detailB]);

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
      { label: 'Total trades', a: String(mA.total_trades), b: String(mB.total_trades) },
      { label: 'Win rate', a: `${mA.win_rate.toFixed(1)}%`, b: `${mB.win_rate.toFixed(1)}%` },
      { label: 'Total PnL (USDT)', a: <SignedMoney value={mA.total_pnl} />, b: <SignedMoney value={mB.total_pnl} /> },
      { label: 'Max drawdown', a: `${mA.max_drawdown.toFixed(1)}%`, b: `${mB.max_drawdown.toFixed(1)}%` },
      { label: 'Sharpe', a: mA.sharpe_ratio.toFixed(2), b: mB.sharpe_ratio.toFixed(2) },
    ];
  }, [detailA, detailB]);

  if (loading) {
    return <CompareLoading />;
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
      <Section title="Choose runs">
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
      </Section>

      {listError !== null && (
        <DataStateErrorCard error={listError} fallback="Failed to load backtest results" onRetry={loadResults} />
      )}

      {detailError !== null && (
        <DataStateErrorCard error={detailError} fallback="Failed to load the selected backtests" />
      )}

      {mismatch.length > 0 && (
        <div className="rounded-lg border border-caution/30 bg-caution-soft px-4 py-3" data-testid="provenance-mismatch">
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
          <Section title="Metrics comparison">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line">
                    <th scope="col" className="pb-2 text-left text-xs font-medium text-ink-2">Metric</th>
                    <th scope="col" className="pb-2 text-right text-xs font-medium text-ink-2">
                      {detailA.name || detailA.strategy}
                    </th>
                    <th scope="col" className="pb-2 text-right text-xs font-medium text-ink-2">
                      {detailB.name || detailB.strategy}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {comparisonMetrics.map((row) => (
                    <tr key={row.label} className="border-b border-line last:border-0">
                      <td className="py-2.5 text-ink-2">{row.label}</td>
                      <td className="py-2.5 text-right font-medium text-ink-1">{row.a}</td>
                      <td className="py-2.5 text-right font-medium text-ink-1">{row.b}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          {/* Overlaid equity curves */}
          <Section title="Equity curves">
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
              <div className="h-72" role="img" aria-label={chartSummary}>
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                    <defs>
                      <linearGradient id="compareGradientA" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor={colors.accent} stopOpacity={0.15} />
                        <stop offset="95%" stopColor={colors.accent} stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id="compareGradientB" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor={colors.caution} stopOpacity={0.15} />
                        <stop offset="95%" stopColor={colors.caution} stopOpacity={0} />
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
                    <Tooltip
                      content={({ active, payload, label }) => {
                        if (!active || !payload?.length) return null;
                        return (
                          <div className="rounded-lg border border-line bg-surface px-3 py-2 shadow-lg">
                            <p className="text-xs text-ink-2">{label != null ? formatXTick(String(label), resolution) : ''}</p>
                            {payload.map((p) => (
                              <p key={String(p.dataKey)} className="text-sm font-medium text-ink-1">
                                {p.dataKey === 'valueA' ? 'A' : 'B'}: {usdt(Number(p.value))}
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
                      stroke={colors.accent}
                      strokeWidth={2}
                      fill="url(#compareGradientA)"
                      dot={false}
                      activeDot={{ r: 4, fill: colors.accent, strokeWidth: 0 }}
                    />
                    <Area
                      type="monotone"
                      dataKey="valueB"
                      stroke={colors.caution}
                      strokeWidth={2}
                      fill="url(#compareGradientB)"
                      dot={false}
                      activeDot={{ r: 4, fill: colors.caution, strokeWidth: 0 }}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <p className="text-sm text-ink-2">No equity points recorded for these runs.</p>
            )}
          </Section>
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

export default function BacktestComparePage() {
  return (
    <Suspense fallback={<CompareLoading />}>
      <BacktestCompareContent />
    </Suspense>
  );
}
