'use client';

import { useEffect, useState, useMemo } from 'react';
import { Play, Download, BarChart3 } from 'lucide-react';
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts';
import { Button } from '@/components/ui/Button';
import { DataCard } from '@/components/ui/DataCard';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { fetchApi } from '@/lib/api';

/* ---------- Types ---------- */

type TimeResolution = 'hours' | 'days' | 'months';

interface BacktestRun {
  id: string;
  strategy: string;
  period: string;
  totalTrades: number;
  winRate: string;
  pnl: string;
  maxDrawdown: string;
  sharpe: string;
  status: string;
  statusVariant: 'success' | 'warning' | 'info' | 'neutral';
}

interface ApiBacktestResult {
  id: string;
  strategy: string;
  period: string;
  status: string;
  metrics: {
    total_trades: number;
    win_rate: number;
    total_pnl: number;
    max_drawdown: number;
    sharpe_ratio: number;
  };
}

interface EquityPoint {
  timestamp: string;
  value: number;
}

interface ApiBacktestDetail {
  id: string;
  strategy: string;
  period: string;
  status: string;
  metrics: {
    total_trades: number;
    win_rate: number;
    total_pnl: number;
    max_drawdown: number;
    sharpe_ratio: number;
  };
  equity_curve: EquityPoint[];
  trades: Array<{
    entry_time: string;
    exit_time: string;
    side: string;
    entry_price: number;
    exit_price: number;
    pnl: number;
  }>;
}

/* ---------- Placeholder data ---------- */

const placeholderRuns: BacktestRun[] = [
  { id: '1', strategy: 'LSTM Momentum', period: 'Jan 1 - Mar 1, 2026', totalTrades: 142, winRate: '67.6%', pnl: '+$3,420.50', maxDrawdown: '8.2%', sharpe: '1.84', status: 'Completed', statusVariant: 'success' },
  { id: '2', strategy: 'Mean Reversion RSI', period: 'Jan 1 - Mar 1, 2026', totalTrades: 98, winRate: '58.2%', pnl: '+$1,180.00', maxDrawdown: '12.4%', sharpe: '1.22', status: 'Completed', statusVariant: 'success' },
  { id: '3', strategy: 'Breakout Scanner', period: 'Feb 1 - Mar 1, 2026', totalTrades: 45, winRate: '42.2%', pnl: '-$340.00', maxDrawdown: '15.8%', sharpe: '0.45', status: 'Completed', statusVariant: 'success' },
  { id: '4', strategy: 'XGBoost Ensemble', period: 'Jan 15 - Mar 1, 2026', totalTrades: 0, winRate: 'N/A', pnl: 'N/A', maxDrawdown: 'N/A', sharpe: 'N/A', status: 'Running', statusVariant: 'info' },
];

function mapApiResults(data: ApiBacktestResult[]): BacktestRun[] {
  return data.map((r) => {
    const m = r.metrics;
    const pnlStr = m.total_pnl !== 0
      ? `${m.total_pnl >= 0 ? '+' : ''}$${Math.abs(m.total_pnl).toLocaleString('en-US', { minimumFractionDigits: 2 })}`
      : 'N/A';
    return {
      id: r.id,
      strategy: r.strategy,
      period: r.period,
      totalTrades: m.total_trades,
      winRate: m.win_rate > 0 ? `${m.win_rate}%` : 'N/A',
      pnl: pnlStr,
      maxDrawdown: m.max_drawdown > 0 ? `${m.max_drawdown}%` : 'N/A',
      sharpe: m.sharpe_ratio > 0 ? m.sharpe_ratio.toFixed(2) : 'N/A',
      status: r.status === 'completed' ? 'Completed' : 'Running',
      statusVariant: r.status === 'completed' ? 'success' : 'info',
    };
  });
}

/* ---------- Recharts helpers ---------- */

function aggregateEquityCurve(data: EquityPoint[], resolution: TimeResolution): EquityPoint[] {
  if (resolution === 'hours') return data;

  const groups = new Map<string, EquityPoint>();
  for (const point of data) {
    const d = new Date(point.timestamp);
    const key = resolution === 'days'
      ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
      : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    // Last value per group wins — iterating in order gives us that for free
    groups.set(key, point);
  }
  return Array.from(groups.values());
}

function formatXTick(timestamp: string, resolution: TimeResolution = 'days'): string {
  const d = new Date(timestamp);
  if (resolution === 'hours') {
    return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
  }
  if (resolution === 'months') {
    return d.toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
  }
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function formatYTick(value: number): string {
  return `$${(value / 1000).toFixed(0)}k`;
}

function makeCustomTooltip(resolution: TimeResolution) {
  return function CustomTooltip({ active, payload, label }: { active?: boolean; payload?: Array<{ value: number }>; label?: string }) {
    if (!active || !payload?.length) return null;
    return (
      <div className="rounded-lg border border-border-default bg-bg-elevated px-3 py-2 shadow-lg">
        <p className="text-xs text-text-muted">{label ? formatXTick(label, resolution) : ''}</p>
        <p className="text-sm font-medium text-text-primary">
          ${payload[0].value.toLocaleString('en-US', { minimumFractionDigits: 2 })}
        </p>
      </div>
    );
  };
}

/* ---------- Page ---------- */

export default function BacktestPage() {
  const [backtestRuns, setBacktestRuns] = useState<BacktestRun[]>(placeholderRuns);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [strategyId, setStrategyId] = useState('strat-lstm-momentum');
  const [startDate, setStartDate] = useState('2026-01-01');
  const [endDate, setEndDate] = useState('2026-03-01');
  const [initialCapital, setInitialCapital] = useState('10000');
  const [pollingTaskId, setPollingTaskId] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<ApiBacktestDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [isDark, setIsDark] = useState(false);
  const [resolution, setResolution] = useState<TimeResolution>('days');

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains('dark'));
  }, []);

  useEffect(() => {
    fetchApi<ApiBacktestResult[]>('/api/backtest/results')
      .then((data) => setBacktestRuns(mapApiResults(data)))
      .catch(() => { /* keep placeholder */ })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!pollingTaskId) return;
    const interval = setInterval(async () => {
      try {
        const status = await fetchApi<{ status: string }>(`/api/backtest/status/${pollingTaskId}`);
        if (status.status === 'completed' || status.status === 'failed') {
          clearInterval(interval);
          setPollingTaskId(null);
          setRunning(false);
          const data = await fetchApi<ApiBacktestResult[]>('/api/backtest/results');
          setBacktestRuns(mapApiResults(data));
        }
      } catch {
        clearInterval(interval);
        setPollingTaskId(null);
        setRunning(false);
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [pollingTaskId]);

  const handleRunBacktest = async () => {
    setRunning(true);
    try {
      const result = await fetchApi<{ task_id: string }>('/api/backtest/run', {
        method: 'POST',
        body: JSON.stringify({
          strategy_id: strategyId,
          start_date: startDate,
          end_date: endDate,
          initial_capital: Number(initialCapital),
        }),
      });
      if (result.task_id) {
        setPollingTaskId(result.task_id);
      } else {
        // No task_id returned — fall back to immediate re-fetch
        const data = await fetchApi<ApiBacktestResult[]>('/api/backtest/results');
        setBacktestRuns(mapApiResults(data));
        setRunning(false);
      }
    } catch {
      /* API unavailable */
      setRunning(false);
    }
  };

  const handleSelectRun = async (id: string) => {
    if (selectedRunId === id) return;
    setSelectedRunId(id);
    setSelectedDetail(null);
    setDetailLoading(true);
    try {
      const detail = await fetchApi<ApiBacktestDetail>(`/api/backtest/results/${id}`);
      setSelectedDetail(detail);
    } catch {
      /* API unavailable — no detail to show */
    } finally {
      setDetailLoading(false);
    }
  };

  const accentColor = isDark ? '#2383e2' : '#2563eb';
  const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)';
  const textMuted = isDark ? 'rgba(255,255,255,0.44)' : '#475569';

  const chartData = useMemo(
    () => selectedDetail ? aggregateEquityCurve(selectedDetail.equity_curve, resolution) : [],
    [selectedDetail, resolution],
  );

  const ChartTooltip = useMemo(() => makeCustomTooltip(resolution), [resolution]);

  const columns = [
    { key: 'strategy', header: 'Strategy' },
    { key: 'period', header: 'Period', hideOnMobile: true },
    { key: 'totalTrades', header: 'Trades', render: (row: BacktestRun) => <span>{row.totalTrades}</span> },
    { key: 'winRate', header: 'Win Rate' },
    {
      key: 'pnl',
      header: 'PnL',
      render: (row: BacktestRun) => (
        <span className={row.pnl.startsWith('+') ? 'text-status-success font-medium' : row.pnl.startsWith('-') ? 'text-status-error font-medium' : 'text-text-muted'}>
          {row.pnl}
        </span>
      ),
    },
    { key: 'sharpe', header: 'Sharpe', hideOnMobile: true },
    {
      key: 'status',
      header: 'Status',
      render: (row: BacktestRun) => (
        <StatusBadge status={row.status} variant={row.statusVariant} size="sm" />
      ),
    },
    {
      key: 'actions',
      header: '',
      render: (row: BacktestRun) => row.status === 'Completed' ? (
        <button
          onClick={() => handleSelectRun(row.id)}
          className={`text-xs hover:underline ${selectedRunId === row.id ? 'text-accent-primary font-medium' : 'text-text-muted'}`}
        >
          View
        </button>
      ) : null,
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      {/* Runner form */}
      <DataCard title="Run Backtest" description="Configure and execute a new backtest run">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <Select
            label="Strategy"
            options={[
              { value: 'strat-lstm-momentum', label: 'LSTM Momentum' },
              { value: 'strat-mean-reversion', label: 'Mean Reversion RSI' },
              { value: 'strat-funding-arb', label: 'Funding Arb' },
              { value: 'strat-breakout', label: 'Breakout Scanner' },
            ]}
            value={strategyId}
            onChange={(e) => setStrategyId(e.target.value)}
            placeholder="Select strategy..."
          />
          <Input label="Start Date" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <Input label="End Date" type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          <Input label="Initial Capital ($)" type="number" value={initialCapital} onChange={(e) => setInitialCapital(e.target.value)} />
        </div>
        <div className="mt-4 flex flex-col gap-3 sm:flex-row">
          <Button variant="primary" onClick={handleRunBacktest} disabled={running}>
            <Play className="h-4 w-4" />
            {pollingTaskId ? 'Polling results...' : running ? 'Running...' : 'Run Backtest'}
          </Button>
          <Button variant="outline">
            <Download className="h-4 w-4" />
            Export Results
          </Button>
        </div>
      </DataCard>

      {/* Results chart */}
      <DataCard title="Backtest Results" description="Equity curve and performance metrics">
        {detailLoading ? (
          <div className="flex h-56 items-center justify-center">
            <p className="text-sm text-text-muted">Loading results...</p>
          </div>
        ) : selectedDetail ? (
          <div className="flex flex-col gap-4">
            {/* Resolution switcher */}
            <div className="flex items-center gap-1">
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

            {/* Equity curve chart */}
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="backtestGradient" x1="0" y1="0" x2="0" y2="1">
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
                  <Tooltip content={<ChartTooltip />} />
                  <Area
                    type="monotone"
                    dataKey="value"
                    stroke={accentColor}
                    strokeWidth={2}
                    fill="url(#backtestGradient)"
                    dot={false}
                    activeDot={{ r: 4, fill: accentColor, strokeWidth: 0 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            {/* Key metrics row */}
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-5 border-t border-border-default pt-4">
              <div>
                <p className="text-xs text-text-muted">Total PnL</p>
                <p className={`text-sm font-semibold ${selectedDetail.metrics.total_pnl >= 0 ? 'text-status-success' : 'text-status-error'}`}>
                  {selectedDetail.metrics.total_pnl >= 0 ? '+' : ''}${Math.abs(selectedDetail.metrics.total_pnl).toLocaleString('en-US', { minimumFractionDigits: 2 })}
                </p>
              </div>
              <div>
                <p className="text-xs text-text-muted">Win Rate</p>
                <p className="text-sm font-semibold text-text-primary">{selectedDetail.metrics.win_rate.toFixed(1)}%</p>
              </div>
              <div>
                <p className="text-xs text-text-muted">Sharpe Ratio</p>
                <p className="text-sm font-semibold text-text-primary">{selectedDetail.metrics.sharpe_ratio.toFixed(2)}</p>
              </div>
              <div>
                <p className="text-xs text-text-muted">Max Drawdown</p>
                <p className="text-sm font-semibold text-status-error">{selectedDetail.metrics.max_drawdown.toFixed(1)}%</p>
              </div>
              <div>
                <p className="text-xs text-text-muted">Total Trades</p>
                <p className="text-sm font-semibold text-text-primary">{selectedDetail.metrics.total_trades}</p>
              </div>
            </div>
          </div>
        ) : (
          <div className="flex h-56 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
            <div className="text-center">
              <div className="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-lg bg-bg-tertiary">
                <BarChart3 className="h-5 w-5 text-text-muted" />
              </div>
              <p className="text-sm font-medium text-text-muted">Backtest Equity Curve</p>
              <p className="mt-1 text-xs text-text-light">
                {loading ? 'Loading...' : 'Select a completed run to view results'}
              </p>
            </div>
          </div>
        )}
      </DataCard>

      {/* Results table */}
      <DataCard title="Backtest History" description="Previous backtest runs">
        <Table
          columns={columns}
          data={backtestRuns}
          keyExtractor={(row) => row.id}
        />
      </DataCard>
    </div>
  );
}
