'use client';

import { useEffect, useState } from 'react';
import { Play, Download, BarChart3 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { DataCard } from '@/components/ui/DataCard';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { fetchApi } from '@/lib/api';

/* ---------- Types ---------- */

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
];

/* ---------- Page ---------- */

export default function BacktestPage() {
  const [backtestRuns, setBacktestRuns] = useState<BacktestRun[]>(placeholderRuns);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    fetchApi<ApiBacktestResult[]>('/api/backtest/results')
      .then((data) => setBacktestRuns(mapApiResults(data)))
      .catch(() => { /* keep placeholder */ })
      .finally(() => setLoading(false));
  }, []);

  const handleRunBacktest = async () => {
    setRunning(true);
    try {
      await fetchApi('/api/backtest/run', {
        method: 'POST',
        body: JSON.stringify({
          strategy_id: 'strat-1',
          start_date: '2026-01-01',
          end_date: '2026-03-01',
          initial_capital: 10000,
        }),
      });
      // Re-fetch results
      const data = await fetchApi<ApiBacktestResult[]>('/api/backtest/results');
      setBacktestRuns(mapApiResults(data));
    } catch {
      /* API unavailable */
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      {/* Runner form */}
      <DataCard title="Run Backtest" description="Configure and execute a new backtest run">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <Select
            label="Strategy"
            options={[
              { value: 'lstm', label: 'LSTM Momentum' },
              { value: 'rsi', label: 'Mean Reversion RSI' },
              { value: 'breakout', label: 'Breakout Scanner' },
              { value: 'xgb', label: 'XGBoost Ensemble' },
            ]}
            placeholder="Select strategy..."
          />
          <Input label="Start Date" type="date" defaultValue="2026-01-01" />
          <Input label="End Date" type="date" defaultValue="2026-03-01" />
          <Input label="Initial Capital ($)" type="number" placeholder="10000" defaultValue="10000" />
        </div>
        <div className="mt-4 flex flex-col gap-3 sm:flex-row">
          <Button variant="primary" onClick={handleRunBacktest} disabled={running}>
            <Play className="h-4 w-4" />
            {running ? 'Running...' : 'Run Backtest'}
          </Button>
          <Button variant="outline">
            <Download className="h-4 w-4" />
            Export Results
          </Button>
        </div>
      </DataCard>

      {/* Results chart placeholder */}
      <DataCard title="Backtest Results" description="Equity curve and performance metrics">
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
