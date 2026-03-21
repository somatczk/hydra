'use client';

import { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { ArrowLeft } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { DataCard } from '@/components/ui/DataCard';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { fetchApi } from '@/lib/api';
import { logger } from '@/lib/logger';

/* ---------- Types ---------- */

interface TrialRecord {
  trial_number: number;
  params: Record<string, number>;
  sharpe: number;
  total_return: number;
  max_drawdown: number;
  total_trades: number;
}

interface HyperoptResult {
  task_id: string;
  status: string;
  best_params: Record<string, number>;
  best_metric: number;
  trials: TrialRecord[];
  total_trials: number;
  completed_trials: number;
}

interface HyperoptProgress {
  task_id: string;
  status: string;
  strategy_id: string;
  completed_trials: number;
  total_trials: number;
  best_so_far: number | null;
  created_at: string;
}

/* ---------- Page ---------- */

export default function HyperoptDetailPage() {
  const params = useParams();
  const router = useRouter();
  const taskId = params.taskId as string;

  const [result, setResult] = useState<HyperoptResult | null>(null);
  const [progress, setProgress] = useState<HyperoptProgress | null>(null);
  const [loading, setLoading] = useState(true);
  const [sortKey, setSortKey] = useState<'sharpe' | 'total_return' | 'max_drawdown' | 'total_trades'>('sharpe');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');

  useEffect(() => {
    async function load() {
      try {
        // Fetch progress info (always available)
        const prog = await fetchApi<HyperoptProgress>(
          `/api/backtest/hyperopt/${taskId}`,
        );
        setProgress(prog);

        // Fetch full results if completed
        if (prog.status === 'completed') {
          const res = await fetchApi<HyperoptResult>(
            `/api/backtest/hyperopt/${taskId}/results`,
          );
          setResult(res);
        }
      } catch (err) {
        logger.warn('HyperoptDetail', 'Failed to load hyperopt run', err);
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [taskId]);

  const handleSort = (key: typeof sortKey) => {
    if (sortKey === key) {
      setSortDir((prev) => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  };

  const sortedTrials = result
    ? [...result.trials].sort((a, b) => {
        const mult = sortDir === 'asc' ? 1 : -1;
        return (a[sortKey] - b[sortKey]) * mult;
      })
    : [];

  const sortIndicator = (key: string) =>
    sortKey === key ? (sortDir === 'desc' ? ' \u2193' : ' \u2191') : '';

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-text-muted">Loading hyperopt run...</p>
      </div>
    );
  }

  if (!progress) {
    return (
      <div className="flex flex-col items-center justify-center gap-4 py-20">
        <p className="text-sm text-text-muted">Hyperopt run not found</p>
        <Button variant="outline" onClick={() => router.push('/optimize')}>
          <ArrowLeft className="h-4 w-4" /> Back to Optimizer
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="sm" onClick={() => router.push('/optimize')}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <h1 className="text-lg font-semibold text-text-primary font-display">
              Hyperopt Run {taskId}
            </h1>
            <p className="text-xs text-text-muted">
              Strategy: {progress.strategy_id}
              {progress.created_at && (
                <> &middot; {new Date(progress.created_at).toLocaleString()}</>
              )}
            </p>
          </div>
        </div>
        <StatusBadge
          status={progress.status === 'completed' ? 'Completed' : progress.status === 'failed' ? 'Failed' : progress.status}
          variant={progress.status === 'completed' ? 'success' : progress.status === 'failed' ? 'error' : 'info'}
        />
      </div>

      {/* Summary metrics */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Trials</p>
          <p className="text-lg font-bold text-text-primary">
            {progress.completed_trials}/{progress.total_trials}
          </p>
        </DataCard>
        <DataCard padding="sm">
          <p className="text-xs text-text-muted">Best Sharpe</p>
          <p className="text-lg font-bold text-text-primary">
            {result ? result.best_metric.toFixed(2) : progress.best_so_far != null ? progress.best_so_far.toFixed(2) : '\u2014'}
          </p>
        </DataCard>
        {result && (
          <>
            <DataCard padding="sm">
              <p className="text-xs text-text-muted">Best Return</p>
              <p className={`text-lg font-bold ${
                Math.max(...result.trials.map((t) => t.total_return)) >= 0
                  ? 'text-status-success'
                  : 'text-status-error'
              }`}>
                {(Math.max(...result.trials.map((t) => t.total_return)) * 100).toFixed(1)}%
              </p>
            </DataCard>
            <DataCard padding="sm">
              <p className="text-xs text-text-muted">Best Params</p>
              <p className="text-xs font-mono text-text-secondary truncate">
                {Object.entries(result.best_params)
                  .map(([k, v]) => `${k}=${v}`)
                  .join(', ')}
              </p>
            </DataCard>
          </>
        )}
      </div>

      {/* Not completed states */}
      {progress.status === 'failed' && (
        <DataCard>
          <p className="text-sm text-status-error">This hyperopt run failed.</p>
        </DataCard>
      )}

      {progress.status !== 'completed' && progress.status !== 'failed' && (
        <DataCard>
          <p className="text-sm text-text-muted">
            This run is still {progress.status}. Results will be available once it completes.
          </p>
        </DataCard>
      )}

      {/* Trials table */}
      {result && sortedTrials.length > 0 && (
        <DataCard
          title="All Trials"
          description={`${result.completed_trials} trials completed`}
        >
          <Table
            columns={[
              {
                key: 'trial_number',
                header: '#',
                render: (r: TrialRecord) => <span className="text-text-muted">{r.trial_number}</span>,
              },
              {
                key: 'params',
                header: 'Parameters',
                render: (r: TrialRecord) => (
                  <span className="text-xs text-text-secondary">
                    {Object.entries(r.params)
                      .map(([k, v]) => `${k}=${v}`)
                      .join(', ')}
                  </span>
                ),
              },
              {
                key: 'sharpe',
                header: `Sharpe${sortIndicator('sharpe')}`,
                render: (r: TrialRecord) => (
                  <span className="cursor-pointer" onClick={() => handleSort('sharpe')}>
                    {r.sharpe.toFixed(2)}
                  </span>
                ),
              },
              {
                key: 'total_return',
                header: `Return${sortIndicator('total_return')}`,
                render: (r: TrialRecord) => (
                  <span
                    className={`cursor-pointer ${r.total_return >= 0 ? 'text-status-success' : 'text-status-error'}`}
                    onClick={() => handleSort('total_return')}
                  >
                    {(r.total_return * 100).toFixed(1)}%
                  </span>
                ),
              },
              {
                key: 'max_drawdown',
                header: `Max DD${sortIndicator('max_drawdown')}`,
                render: (r: TrialRecord) => (
                  <span
                    className="text-status-error cursor-pointer"
                    onClick={() => handleSort('max_drawdown')}
                  >
                    {(r.max_drawdown * 100).toFixed(1)}%
                  </span>
                ),
              },
              {
                key: 'total_trades',
                header: `Trades${sortIndicator('total_trades')}`,
                render: (r: TrialRecord) => (
                  <span className="cursor-pointer" onClick={() => handleSort('total_trades')}>
                    {r.total_trades}
                  </span>
                ),
              },
            ]}
            data={sortedTrials}
            keyExtractor={(r) => String(r.trial_number)}
          />
        </DataCard>
      )}
    </div>
  );
}
