'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Play, ArrowRight, Eye } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { DataCard } from '@/components/ui/DataCard';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Table } from '@/components/ui/Table';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { ErrorCard } from '@/components/ui/ErrorCard';
import { DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { ProvenancePanel, type DataProvenance } from '@/components/backtest/DataProvenance';
import { describeApiError, parseErrorDetail } from '@/lib/apiState';
import { logger } from '@/lib/logger';
import { ResearchNav } from '@/components/research/ResearchNav';

/* ---------- Types ---------- */

interface StrategyOption {
  id: string;
  name: string;
}

interface ParamRange {
  name: string;
  label: string;
  min: number;
  max: number;
  step: number;
  type: string;
}

interface StrategyParamDef {
  name: string;
  label: string;
  type: string;
  default: number;
  min: number;
  max: number;
  step: number;
}

interface OptimizeResult {
  id: string;
  params: Record<string, number>;
  pnl: number;
  win_rate: number;
  sharpe: number;
  max_drawdown: number;
}

interface HyperoptHistoryEntry {
  task_id: string;
  status: string;
  strategy_id: string;
  completed_trials: number;
  total_trials: number;
  best_so_far: number | null;
  created_at: string;
  error?: string | null;
}


/* ---------- Helpers ---------- */

function fmtPnl(v: number): string {
  const sign = v >= 0 ? '+' : '-';
  return `${sign}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
}

/* ---------- Page ---------- */

export default function OptimizePage() {
  const router = useRouter();
  const { toast } = useToast();
  const [strategies, setStrategies] = useState<StrategyOption[]>([]);
  const [strategyId, setStrategyId] = useState('');
  const [loading, setLoading] = useState(true);
  const [strategiesError, setStrategiesError] = useState<unknown>(null);
  const [paramsError, setParamsError] = useState<string | null>(null);
  const [runError, setRunError] = useState<unknown>(null);
  const [runFailure, setRunFailure] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState(0);
  const [pollingTaskId, setPollingTaskId] = useState<string | null>(() => {
    if (typeof window !== 'undefined') return localStorage.getItem('hyperopt_task_id');
    return null;
  });
  const [maxTrials, setMaxTrials] = useState('100');
  const [startDate, setStartDate] = useState('2025-01-01');
  const [endDate, setEndDate] = useState('2026-03-01');
  const [trialStatus, setTrialStatus] = useState<{
    completed: number;
    total: number;
    bestSoFar: number | null;
    lastSharpe: number | null;
    currentParams: Record<string, number> | null;
    startDate: string;
    endDate: string;
    provenance: DataProvenance | null;
  } | null>(null);
  const [params, setParams] = useState<ParamRange[]>([]);
  const [paramsLoading, setParamsLoading] = useState(false);
  const [results, setResults] = useState<OptimizeResult[]>([]);
  const [sortKey, setSortKey] = useState<'pnl' | 'win_rate' | 'sharpe' | 'max_drawdown'>('pnl');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [history, setHistory] = useState<HyperoptHistoryEntry[]>([]);

  useEffect(() => {
    fetchApi<HyperoptHistoryEntry[]>('/api/backtest/hyperopt/history')
      .then(setHistory)
      .catch((err) => logger.warn('Optimize', 'Failed to fetch hyperopt history', err));
  }, []);

  // Sync pollingTaskId to localStorage
  useEffect(() => {
    if (pollingTaskId) {
      localStorage.setItem('hyperopt_task_id', pollingTaskId);
      setRunning(true);
    } else {
      localStorage.removeItem('hyperopt_task_id');
    }
  }, [pollingTaskId]);

  useEffect(() => {
    fetchApi<StrategyOption[]>('/api/backtest/strategies')
      .then((data) => {
        const mapped = data.map((s: { id: string; name: string }) => ({ id: s.id, name: s.name }));
        setStrategies(mapped);
        if (mapped.length > 0) setStrategyId(mapped[0].id);
      })
      .catch((err) => {
        logger.warn('Optimize', 'Failed to fetch strategies', err);
        setStrategiesError(err);
      })
      .finally(() => setLoading(false));
  }, []);

  // Fetch optimizable params when strategy changes
  useEffect(() => {
    if (!strategyId) {
      setParams([]);
      return;
    }
    setParamsLoading(true);
    setParamsError(null);
    fetchApi<StrategyParamDef[]>(`/api/backtest/strategies/${strategyId}/params`)
      .then((defs) => {
        setParams(
          defs.map((d) => ({
            name: d.name,
            label: d.label,
            min: d.min,
            max: d.max,
            step: d.step,
            type: d.type,
          })),
        );
      })
      .catch((err) => {
        logger.warn('Optimize', 'Failed to fetch strategy params', err);
        setParams([]);
        setParamsError(
          parseErrorDetail(err)?.state === 'not_found'
            ? 'Strategy not found'
            : describeApiError(err, 'Failed to load strategy parameters'),
        );
      })
      .finally(() => setParamsLoading(false));
  }, [strategyId]);

  const updateParam = (index: number, field: 'min' | 'max' | 'step', value: string) => {
    setParams((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], [field]: parseFloat(value) || 0 };
      return next;
    });
  };

  // Poll optimization status
  useEffect(() => {
    if (!pollingTaskId) return;
    const interval = setInterval(async () => {
      try {
        const status = await fetchApi<{
          status: string;
          completed_trials: number;
          total_trials: number;
          best_so_far: number | null;
          current_trial_params: Record<string, number> | null;
          last_trial_sharpe: number | null;
          start_date: string;
          end_date: string;
          error?: string | null;
          provenance?: DataProvenance | null;
        }>(`/api/backtest/hyperopt/${pollingTaskId}`);
        setProgress(
          status.total_trials > 0
            ? Math.round((status.completed_trials / status.total_trials) * 100)
            : 0,
        );
        setTrialStatus({
          completed: status.completed_trials,
          total: status.total_trials,
          bestSoFar: status.best_so_far,
          lastSharpe: status.last_trial_sharpe,
          currentParams: status.current_trial_params,
          startDate: status.start_date,
          endDate: status.end_date,
          provenance: status.provenance ?? null,
        });
        if (status.status === 'completed' || status.status === 'failed') {
          clearInterval(interval);
          setPollingTaskId(null);
          setRunning(false);
          setProgress(0);
          setTrialStatus(null);
          // Refresh history list
          fetchApi<HyperoptHistoryEntry[]>('/api/backtest/hyperopt/history')
            .then(setHistory)
            .catch(() => {});
          if (status.status === 'completed') {
            toast('success', 'Optimization completed');
          } else {
            const reason = status.error || 'no reason reported';
            setRunFailure(`Optimization failed: ${reason}`);
            toast('error', `Optimization failed: ${reason}`);
          }
        }
      } catch (err) {
        setRunFailure(
          parseErrorDetail(err)?.state === 'not_found'
            ? 'The optimization task is no longer known to the server (it may have restarted).'
            : describeApiError(err, 'Lost contact with the optimization task'),
        );
        clearInterval(interval);
        setPollingTaskId(null);
        setRunning(false);
        setProgress(0);
        setTrialStatus(null);
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [pollingTaskId, toast]);

  const handleRun = async () => {
    if (!strategyId || !startDate || !endDate) return;
    setRunning(true);
    setRunError(null);
    setRunFailure(null);
    setResults([]);
    try {
      const paramSpace = params.map((p) => ({
        name: p.name,
        type: p.type || 'float',
        low: p.min,
        high: p.max,
      }));
      const result = await fetchApi<{ task_id?: string; results?: OptimizeResult[] }>(
        '/api/backtest/hyperopt',
        {
          method: 'POST',
          body: JSON.stringify({
            strategy_id: strategyId,
            param_space: paramSpace,
            max_trials: parseInt(maxTrials, 10) || 100,
            start_date: startDate,
            end_date: endDate,
          }),
        },
      );
      toast('info', 'Optimization started');
      if (result.task_id) {
        setPollingTaskId(result.task_id);
      } else if (result.results) {
        setResults(result.results);
        setRunning(false);
        toast('success', 'Optimization completed');
      } else {
        setRunning(false);
      }
    } catch (err) {
      logger.error('Optimize', 'Failed to start optimization', err);
      setRunError(err);
      toast('error', describeApiError(err, 'Failed to start optimization'));
      setRunning(false);
    }
  };

  const handleSort = (key: typeof sortKey) => {
    if (sortKey === key) {
      setSortDir((prev) => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  };

  const sortedResults = [...results].sort((a, b) => {
    const mult = sortDir === 'asc' ? 1 : -1;
    return (a[sortKey] - b[sortKey]) * mult;
  });

  const handleApplyBest = () => {
    if (sortedResults.length === 0) return;
    router.push(`/builder?strategy=${strategyId}`);
  };


  const columns = [
    {
      key: 'params',
      header: 'Parameters',
      render: (row: OptimizeResult) => (
        <span className="text-xs text-ink-2">
          {Object.entries(row.params)
            .map(([k, v]) => `${k}=${v}`)
            .join(', ')}
        </span>
      ),
    },
    {
      key: 'pnl',
      header: `PnL${sortKey === 'pnl' ? (sortDir === 'desc' ? ' \u2193' : ' \u2191') : ''}`,
      render: (row: OptimizeResult) => (
        <span
          className={row.pnl >= 0 ? 'text-ink-1 font-medium cursor-pointer' : 'text-danger font-medium cursor-pointer'}
          onClick={() => handleSort('pnl')}
        >
          {fmtPnl(row.pnl)}
        </span>
      ),
    },
    {
      key: 'win_rate',
      header: `Win Rate${sortKey === 'win_rate' ? (sortDir === 'desc' ? ' \u2193' : ' \u2191') : ''}`,
      render: (row: OptimizeResult) => (
        <span className="cursor-pointer" onClick={() => handleSort('win_rate')}>
          {row.win_rate.toFixed(1)}%
        </span>
      ),
    },
    {
      key: 'sharpe',
      header: `Sharpe${sortKey === 'sharpe' ? (sortDir === 'desc' ? ' \u2193' : ' \u2191') : ''}`,
      render: (row: OptimizeResult) => (
        <span className="cursor-pointer" onClick={() => handleSort('sharpe')}>
          {row.sharpe.toFixed(2)}
        </span>
      ),
    },
    {
      key: 'max_drawdown',
      header: `Max DD${sortKey === 'max_drawdown' ? (sortDir === 'desc' ? ' \u2193' : ' \u2191') : ''}`,
      render: (row: OptimizeResult) => (
        <span className="text-danger cursor-pointer" onClick={() => handleSort('max_drawdown')}>
          {row.max_drawdown.toFixed(1)}%
        </span>
      ),
    },
  ];

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-ink-2">Loading...</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <ResearchNav current="/optimize" />
      <div>
        <h1 className="text-xl font-semibold text-ink-1">Strategy optimizer</h1>
        <p className="text-sm text-ink-2 mt-1">
          Find optimal parameter combinations for your strategy
        </p>
      </div>

      {strategiesError !== null && (
        <DataStateErrorCard error={strategiesError} fallback="Failed to load strategies" />
      )}

      {/* Configuration */}
      <DataCard title="Configuration" description="Select strategy and define parameter search space">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4 mb-6">
          <Select
            label="Strategy"
            options={strategies.map((s) => ({ value: s.id, label: s.name }))}
            value={strategyId}
            onChange={(e) => setStrategyId(e.target.value)}
            placeholder="Select strategy..."
          />
          <Input
            label="Max Trials"
            type="number"
            value={maxTrials}
            onChange={(e) => setMaxTrials(e.target.value)}
          />
          <Input
            label="Start Date"
            type="date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
          />
          <Input
            label="End Date"
            type="date"
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
          />
        </div>

        {/* Parameter ranges */}
        <div className="space-y-3">
          <p className="text-xs font-medium text-ink-2">Parameter Ranges</p>
          {paramsLoading && (
            <p className="text-xs text-ink-2">Loading parameters...</p>
          )}
          {paramsError && <p className="text-xs text-danger">{paramsError}</p>}
          {!paramsLoading && !paramsError && params.length === 0 && strategyId && (
            <p className="text-xs text-ink-2">No optimizable parameters found for this strategy.</p>
          )}
          {params.map((param, i) => (
            <div key={param.name} className="grid grid-cols-4 gap-3 items-end">
              <div>
                <label className="block text-xs text-ink-2 mb-1">{param.label}</label>
              </div>
              <Input
                label="Min"
                type="number"
                value={String(param.min)}
                onChange={(e) => updateParam(i, 'min', e.target.value)}
              />
              <Input
                label="Max"
                type="number"
                value={String(param.max)}
                onChange={(e) => updateParam(i, 'max', e.target.value)}
              />
              <Input
                label="Step"
                type="number"
                value={String(param.step)}
                onChange={(e) => updateParam(i, 'step', e.target.value)}
              />
            </div>
          ))}
        </div>

        <div className="mt-4 flex flex-col gap-3">
          {runError !== null && (
            <div data-testid="run-error">
              <DataStateErrorCard error={runError} fallback="Failed to start optimization" />
            </div>
          )}
          {runFailure && (
            <div data-testid="run-failure">
              <ErrorCard message={runFailure} />
            </div>
          )}
          <Button
            variant="primary"
            onClick={handleRun}
            disabled={running || !strategyId || params.length === 0 || !startDate || !endDate}
          >
            <Play className="h-4 w-4" />
            {running ? `Running${progress > 0 ? ` (${progress}%)` : '...'}` : 'Run Optimization'}
          </Button>
          {running && (
            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <div className="flex-1 h-2 rounded-full bg-raised overflow-hidden">
                  <div
                    className="h-full rounded-full bg-accent transition-all duration-500 ease-out"
                    style={{ width: `${progress}%` }}
                  />
                </div>
                <span className="text-xs text-ink-2 w-10 text-right">{progress}%</span>
              </div>
              {trialStatus && (
                <div className="rounded-lg border border-line bg-surface p-3 space-y-2">
                  <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs">
                    <div className="flex justify-between">
                      <span className="text-ink-2">Trial</span>
                      <span className="font-mono text-ink-1">{trialStatus.completed} / {trialStatus.total}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-ink-2">Best Sharpe</span>
                      <span className={`font-mono ${trialStatus.bestSoFar != null && trialStatus.bestSoFar > 0 ? 'text-ink-1' : 'text-ink-1'}`}>
                        {trialStatus.bestSoFar != null ? trialStatus.bestSoFar.toFixed(3) : '—'}
                      </span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-ink-2">Last Trial Sharpe</span>
                      <span className="font-mono text-ink-2">
                        {trialStatus.lastSharpe != null ? trialStatus.lastSharpe.toFixed(3) : '—'}
                      </span>
                    </div>
                    {trialStatus.startDate && trialStatus.endDate && (
                      <div className="flex justify-between">
                        <span className="text-ink-2">Period</span>
                        <span className="font-mono text-ink-2">{trialStatus.startDate} — {trialStatus.endDate}</span>
                      </div>
                    )}
                  </div>
                  {trialStatus.provenance && <ProvenancePanel provenance={trialStatus.provenance} />}
                  {trialStatus.currentParams && (
                    <div className="pt-1 border-t border-line">
                      <p className="text-[10px] text-ink-2 mb-1">Last Trial Parameters</p>
                      <div className="flex flex-wrap gap-2">
                        {Object.entries(trialStatus.currentParams).map(([k, v]) => (
                          <span key={k} className="inline-flex items-center gap-1 rounded bg-raised px-1.5 py-0.5 text-[10px] font-mono text-ink-2">
                            {k}=<span className="text-ink-1">{typeof v === 'number' ? v.toFixed(2) : v}</span>
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </DataCard>

      {/* Results */}
      {results.length > 0 && (
        <DataCard title="Results" description={`${results.length} parameter combinations tested`}>
          <div className="mb-3 flex justify-end">
            <Button variant="primary" size="sm" onClick={handleApplyBest}>
              <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
              Open in Builder
            </Button>
          </div>
          <Table
            columns={columns}
            data={sortedResults}
            keyExtractor={(row) => row.id}
          />
        </DataCard>
      )}

      {/* History */}
      {history.length > 0 && (
        <DataCard title="Optimization History" description="Previously completed hyperopt runs">
          <Table
            columns={[
              { key: 'task_id', header: 'Run ID', render: (r: HyperoptHistoryEntry) => <span className="text-xs font-mono">{r.task_id}</span> },
              { key: 'strategy_id', header: 'Strategy', render: (r: HyperoptHistoryEntry) => <span>{r.strategy_id}</span> },
              {
                key: 'status',
                header: 'Status',
                render: (r: HyperoptHistoryEntry) => (
                  <span className={r.status === 'completed' ? 'text-ink-1' : r.status === 'failed' ? 'text-danger' : 'text-ink-2'}>
                    {r.status}
                  </span>
                ),
              },
              {
                key: 'trials',
                header: 'Trials',
                render: (r: HyperoptHistoryEntry) => <span>{r.completed_trials}/{r.total_trials}</span>,
              },
              {
                key: 'best_so_far',
                header: 'Best Sharpe',
                render: (r: HyperoptHistoryEntry) => <span>{r.best_so_far != null ? r.best_so_far.toFixed(2) : '—'}</span>,
              },
              {
                key: 'created_at',
                header: 'Date',
                render: (r: HyperoptHistoryEntry) => (
                  <span className="text-xs text-ink-2">
                    {r.created_at ? new Date(r.created_at).toLocaleString() : '—'}
                  </span>
                ),
              },
              {
                key: 'actions',
                header: '',
                render: (r: HyperoptHistoryEntry) =>
                  r.status === 'completed' ? (
                    <Button variant="ghost" size="sm" onClick={() => router.push(`/optimize/${r.task_id}`)}>
                      <Eye className="h-3.5 w-3.5" />
                      View
                    </Button>
                  ) : null,
              },
            ]}
            data={history}
            keyExtractor={(r) => r.task_id}
          />
        </DataCard>
      )}
    </div>
  );
}
