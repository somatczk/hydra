'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Play, Download, Trash2 } from 'lucide-react';
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
  name: string;
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
  name: string;
  metrics: {
    total_trades: number;
    win_rate: number;
    total_pnl: number;
    max_drawdown: number;
    sharpe_ratio: number;
  };
}

/* ---------- Helpers ---------- */

function mapApiResults(data: ApiBacktestResult[]): BacktestRun[] {
  return data.map((r) => {
    const m = r.metrics;
    const pnlStr = m.total_pnl !== 0
      ? `${m.total_pnl >= 0 ? '+' : '-'}$${Math.abs(m.total_pnl).toLocaleString('en-US', { minimumFractionDigits: 2 })}`
      : 'N/A';
    return {
      id: r.id,
      strategy: r.strategy,
      period: r.period,
      name: r.name || '',
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

/* ---------- Page ---------- */

export default function BacktestPage() {
  const router = useRouter();
  const [backtestRuns, setBacktestRuns] = useState<BacktestRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [strategyId, setStrategyId] = useState('strat-lstm-momentum');
  const [startDate, setStartDate] = useState('2026-01-01');
  const [endDate, setEndDate] = useState('2026-03-01');
  const [initialCapital, setInitialCapital] = useState('10000');
  const [backtestName, setBacktestName] = useState('');
  const [pollingTaskId, setPollingTaskId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const fetchResults = async () => {
    try {
      const data = await fetchApi<ApiBacktestResult[]>('/api/backtest/results');
      setBacktestRuns(mapApiResults(data));
    } catch {
      /* API unavailable */
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchResults(); }, []);

  useEffect(() => {
    if (!pollingTaskId) return;
    const interval = setInterval(async () => {
      try {
        const status = await fetchApi<{ status: string }>(`/api/backtest/status/${pollingTaskId}`);
        if (status.status === 'completed' || status.status === 'failed') {
          clearInterval(interval);
          setPollingTaskId(null);
          setRunning(false);
          fetchResults();
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
          name: backtestName,
        }),
      });
      if (result.task_id) {
        setPollingTaskId(result.task_id);
      } else {
        fetchResults();
        setRunning(false);
      }
    } catch {
      setRunning(false);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await fetchApi<undefined>(`/api/backtest/results/${id}`, { method: 'DELETE' });
      setBacktestRuns((prev) => prev.filter((r) => r.id !== id));
    } catch {
      /* API unavailable */
    } finally {
      setDeletingId(null);
    }
  };

  const columns = [
    {
      key: 'name',
      header: 'Name',
      render: (row: BacktestRun) => (
        <span className="text-text-primary">{row.name || row.strategy}</span>
      ),
    },
    { key: 'strategy', header: 'Strategy', hideOnMobile: true },
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
      render: (row: BacktestRun) => (
        <div className="flex items-center gap-2">
          {row.status === 'Completed' && (
            <button
              onClick={(e) => { e.stopPropagation(); router.push(`/backtest/${row.id}`); }}
              className="text-xs text-text-muted hover:underline"
            >
              View
            </button>
          )}
          {deletingId === row.id ? (
            <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
              <button onClick={() => handleDelete(row.id)} className="text-xs text-status-error hover:underline">Confirm</button>
              <button onClick={() => setDeletingId(null)} className="text-xs text-text-muted hover:underline">Cancel</button>
            </div>
          ) : (
            <button
              onClick={(e) => { e.stopPropagation(); setDeletingId(row.id); }}
              className="text-text-muted hover:text-status-error transition-colors"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      {/* Runner form */}
      <DataCard title="Run Backtest" description="Configure and execute a new backtest run">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-5">
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
          <Input label="Name (optional)" type="text" value={backtestName} onChange={(e) => setBacktestName(e.target.value)} placeholder="e.g. LSTM Q1 run" />
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

      {/* Results table */}
      <DataCard title="Backtest History" description="Previous backtest runs">
        <Table
          columns={columns}
          data={backtestRuns}
          keyExtractor={(row) => row.id}
          loading={loading}
        />
      </DataCard>
    </div>
  );
}
