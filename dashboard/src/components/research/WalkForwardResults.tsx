'use client';

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { TooltipContentProps, TooltipValueType } from 'recharts';
import { Section } from '@/components/ui/Section';
import { Table } from '@/components/ui/Table';
import { ProvenancePanel, type DataProvenance } from '@/components/backtest/DataProvenance';
import { money, pct } from '@/lib/format';
import { utcDay, wfFoldRow, type WfFold } from '@/lib/research';
import { chartColors, formatYTick } from '@/app/backtest/chart-utils';

/** One fold as `GET /api/backtest/walkforward/{id}/results` returns it. */
export interface WfFoldMetrics extends WfFold {
  in_sample_sharpe: number;
  in_sample_return: number;
  in_sample_drawdown: number;
  in_sample_trades: number;
  out_of_sample_sharpe: number;
  out_of_sample_return: number;
  out_of_sample_drawdown: number;
  out_of_sample_trades: number;
}

export interface WalkForwardResult {
  task_id: string;
  status: string;
  folds: WfFoldMetrics[];
  aggregated_sharpe: number | null;
  aggregated_return: number | null;
  aggregated_drawdown: number | null;
  out_of_sample_equity: number[];
  oos_psr: number | null;
  efficiency: number | null;
  anchored: boolean;
  seed: number;
  provenance?: DataProvenance | null;
}

const dp2 = (v: number) => v.toFixed(2);
const metric = (v: number | null, fmt: (n: number) => string) =>
  typeof v === 'number' ? fmt(v) : '—';

/** Summary lines: only the aggregates the API returned; nothing is 0-from-null. */
function summaryLines(r: WalkForwardResult): string[] {
  const lines: string[] = [];
  if (typeof r.aggregated_sharpe === 'number') lines.push(`OOS Sharpe ${dp2(r.aggregated_sharpe)}`);
  if (typeof r.aggregated_return === 'number') {
    lines.push(`OOS return ${metric(r.aggregated_return, (n) => pct(n * 100) ?? '—')}`);
  }
  if (typeof r.aggregated_drawdown === 'number') {
    lines.push(`OOS max DD ${metric(r.aggregated_drawdown, (n) => pct(n * 100) ?? '—')}`);
  }
  if (typeof r.oos_psr === 'number') lines.push(`OOS PSR ${dp2(r.oos_psr)}`);
  if (typeof r.efficiency === 'number') lines.push(`Efficiency ${dp2(r.efficiency)}`);
  lines.push(r.anchored ? 'Anchored windows' : 'Rolling windows');
  lines.push(`Seed ${r.seed}`);
  return lines;
}

/** Fold-boundary tick positions and labels; per-fold point counts are not on the wire, so they are even. */
export function foldTicks(r: WalkForwardResult): { ticks: number[]; label: (i: number) => string } {
  const points = r.out_of_sample_equity.length;
  const last = points - 1;
  const labels = new Map<number, string>();
  r.folds.forEach((f, k) => {
    const start = f.oos_start;
    if (!start) return;
    labels.set(k === 0 ? 1 : Math.round(1 + (k * last) / r.folds.length), utcDay(start));
  });
  const lastFold = r.folds[r.folds.length - 1];
  // a colliding index keeps the fold's start label; the end label is the one that yields
  if (last >= 0 && lastFold?.oos_end && !labels.has(last)) labels.set(last, utcDay(lastFold.oos_end));
  return {
    ticks: [...labels.keys()].sort((a, b) => a - b),
    label: (i: number) => labels.get(i) ?? String(i),
  };
}

const foldColumns = [
  {
    key: 'fold',
    header: 'Fold',
    render: (f: WfFoldMetrics) => <span className="text-ink-2">{f.fold_index + 1}</span>,
  },
  {
    key: 'is_window',
    header: 'IS window',
    render: (f: WfFoldMetrics) => (
      <span className="text-xs text-ink-2">{wfFoldRow(f).isWindow}</span>
    ),
  },
  {
    key: 'is_sharpe',
    header: 'IS Sharpe',
    numeric: true,
    hideOnMobile: true,
    render: (f: WfFoldMetrics) => <span>{dp2(f.in_sample_sharpe)}</span>,
  },
  {
    key: 'is_return',
    header: 'IS Return',
    numeric: true,
    hideOnMobile: true,
    render: (f: WfFoldMetrics) => <span>{metric(f.in_sample_return, (n) => pct(n * 100) ?? '—')}</span>,
  },
  {
    key: 'oos_window',
    header: 'OOS window',
    render: (f: WfFoldMetrics) => (
      <span className="text-xs text-ink-2">{wfFoldRow(f).oosWindow}</span>
    ),
  },
  {
    key: 'oos_sharpe',
    header: 'OOS Sharpe',
    numeric: true,
    render: (f: WfFoldMetrics) => <span className="font-medium">{dp2(f.out_of_sample_sharpe)}</span>,
  },
  {
    key: 'oos_return',
    header: 'OOS Return',
    numeric: true,
    hideOnMobile: true,
    render: (f: WfFoldMetrics) => (
      <span>{metric(f.out_of_sample_return, (n) => pct(n * 100) ?? '—')}</span>
    ),
  },
  {
    key: 'oos_drawdown',
    header: 'OOS Max DD',
    numeric: true,
    hideOnMobile: true,
    render: (f: WfFoldMetrics) => (
      <span className="text-danger">
        {metric(f.out_of_sample_drawdown, (n) => pct(n * 100) ?? '—')}
      </span>
    ),
  },
  {
    key: 'trials',
    header: 'Trials',
    numeric: true,
    render: (f: WfFoldMetrics) => <span>{f.n_trials ?? '—'}</span>,
  },
  {
    key: 'params',
    header: 'Selected params',
    render: (f: WfFoldMetrics) => (
      <span className="font-mono text-xs text-ink-2">{wfFoldRow(f).params}</span>
    ),
  },
];

/** A completed walk-forward run: aggregate readouts, the stitched OOS equity curve and the folds table. */
export function WalkForwardResults({ result }: { result: WalkForwardResult }) {
  const colors = chartColors();
  const points = result.out_of_sample_equity;
  const { ticks, label } = foldTicks(result);
  const chartData = points.map((value, index) => ({ index, value }));
  const first = points[0];
  const last = points[points.length - 1];
  const summary =
    points.length > 0
      ? `Out-of-sample equity stitched across ${result.folds.length} folds: start ${money(first) ?? '—'} USDT, end ${money(last) ?? '—'} USDT.`
      : null;

  return (
    <>
      <Section title="Walk-forward result">
        <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm" data-testid="wf-summary">
          {summaryLines(result).map((line) => (
            <span key={line} className="text-ink-2">
              {line}
            </span>
          ))}
        </div>
        {result.provenance && <div className="mt-3"><ProvenancePanel provenance={result.provenance} /></div>}
        {summary && points.length > 1 ? (
          <div className="mt-3 h-64" role="img" aria-label={summary}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={colors.grid} vertical={false} />
                <XAxis
                  dataKey="index"
                  type="number"
                  domain={[0, points.length - 1]}
                  ticks={ticks}
                  tickFormatter={label}
                  tick={{ fontSize: 11, fill: colors.text }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tickFormatter={formatYTick}
                  tick={{ fontSize: 11, fill: colors.text }}
                  axisLine={false}
                  tickLine={false}
                  width={44}
                  label={{
                    value: 'USDT',
                    angle: -90,
                    position: 'insideLeft',
                    fill: colors.text,
                    fontSize: 11,
                  }}
                />
                <Tooltip
                  content={({ active, payload }: TooltipContentProps<TooltipValueType, string | number>) =>
                    active && payload?.length ? (
                      <div className="rounded-lg border border-line bg-surface px-3 py-2">
                        <p className="text-sm font-medium text-ink-1">
                          {money(Number(payload[0].value)) ?? '—'} USDT
                        </p>
                      </div>
                    ) : null
                  }
                />
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke={colors.accent}
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4, fill: colors.accent, strokeWidth: 0 }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="mt-3 text-sm text-ink-2">No out-of-sample equity points recorded.</p>
        )}
      </Section>
      <Section title="Folds">
        <Table
          columns={foldColumns}
          data={result.folds}
          keyExtractor={(f) => String(f.fold_index)}
          emptyMessage="No folds recorded"
        />
      </Section>
    </>
  );
}
