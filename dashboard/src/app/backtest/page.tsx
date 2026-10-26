'use client';

import { useEffect, useRef, useState, Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Play, Download, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Section } from '@/components/ui/Section';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { ErrorCard } from '@/components/ui/ErrorCard';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { ProvenanceBadge, type DataProvenance } from '@/components/backtest/DataProvenance';
import { describeApiError } from '@/lib/apiState';
import { logger } from '@/lib/logger';
import { signed } from '@/lib/format';
import { usePoll } from '@/hooks/usePoll';
import { ResearchNav } from '@/components/research/ResearchNav';
import { JobRow } from '@/components/research/JobRow';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import {
  jobStatusText,
  jobStatusTone,
  noStoredBars,
  type ResearchJob,
} from '@/lib/research';
import Link from 'next/link';

/* ---------- Types ---------- */

interface BacktestRun {
  id: string;
  strategy: string;
  period: string;
  name: string;
  totalTrades: number;
  winRate: string | null;
  pnl: string | null;
  pnlNegative: boolean;
  maxDrawdown: string | null;
  sharpe: string | null;
  status: string;
  statusVariant: 'success' | 'warning' | 'info' | 'neutral' | 'error';
  provenance: DataProvenance | null;
}

interface ApiBacktestResult {
  id: string;
  strategy: string;
  period: string;
  status: string;
  name: string;
  strategy_id?: string | null;
  provenance?: DataProvenance | null;
  metrics: {
    total_trades: number;
    win_rate: number | null;
    total_pnl: number | null;
    max_drawdown: number | null;
    sharpe_ratio: number | null;
  };
}

interface StrategyOption {
  id: string;
  name: string;
  primary_timeframe?: string | null;
}

const TIMEFRAME_OPTIONS = [
  { value: '1m', label: '1 Minute' },
  { value: '5m', label: '5 Minutes' },
  { value: '15m', label: '15 Minutes' },
  { value: '1h', label: '1 Hour' },
  { value: '4h', label: '4 Hours' },
  { value: '1d', label: '1 Day' },
  { value: '1w', label: '1 Week' },
];

function statusDisplay(status: string): Pick<BacktestRun, 'status' | 'statusVariant'> {
  if (status === 'completed') return { status: jobStatusText(status), statusVariant: 'success' };
  const tone = jobStatusTone(status);
  return {
    status: jobStatusText(status),
    statusVariant: tone === 'danger' ? 'error' : tone === 'caution' ? 'warning' : 'info',
  };
}

/* ---------- Helpers ---------- */

/** A missing metric is NO DATA; a real zero or a loss is shown as it is. */
const metric = (v: string | null) => v ?? <span className="text-ink-2">— NO DATA</span>;

function mapApiResults(data: ApiBacktestResult[]): BacktestRun[] {
  return data.map((r) => {
    const m = r.metrics;
    return {
      id: r.id,
      strategy: r.strategy,
      period: r.period,
      name: r.name || '',
      totalTrades: m.total_trades,
      winRate: m.win_rate === null ? null : `${m.win_rate}%`,
      pnl: signed(m.total_pnl),
      pnlNegative: m.total_pnl !== null && m.total_pnl < 0,
      maxDrawdown: m.max_drawdown === null ? null : `${m.max_drawdown}%`,
      sharpe: m.sharpe_ratio === null ? null : m.sharpe_ratio.toFixed(2),
      ...statusDisplay(r.status),
      provenance: r.provenance ?? null,
    };
  });
}

/* ---------- Page ---------- */

function BacktestPageContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const { toast } = useToast();
  const [backtestRuns, setBacktestRuns] = useState<BacktestRun[]>([]);
  const [resultsError, setResultsError] = useState<unknown>(null);
  const [runError, setRunError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [strategies, setStrategies] = useState<StrategyOption[]>([]);
  const [strategyId, setStrategyId] = useState(searchParams.get('strategy') || '');
  const [strategiesError, setStrategiesError] = useState(false);
  const [startDate, setStartDate] = useState('2026-01-01');
  const [endDate, setEndDate] = useState('2026-03-01');
  const [timeframe, setTimeframe] = useState('1h');
  const [initialCapital, setInitialCapital] = useState('10000');
  const [backtestName, setBacktestName] = useState('');
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const autorunTriggered = useRef(false);

  // Durable rows straight from the job queue; nothing about a run lives in this page's state.
  const jobs = usePoll<ResearchJob[]>('/api/backtest/jobs?kind=backtest&limit=50', 10000);

  const fetchResults = async () => {
    try {
      const data = await fetchApi<ApiBacktestResult[]>('/api/backtest/results');
      setBacktestRuns(mapApiResults(data));
      setResultsError(null);
    } catch (err) {
      logger.warn('Backtest', 'Failed to load backtest results', err);
      setResultsError(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchResults(); }, []);

  /* Fetch available strategies for the dropdown */
  const fetchStrategies = () => {
    setStrategiesError(false);
    fetchApi<StrategyOption[]>('/api/backtest/strategies')
      .then((data) => {
        if (data.length > 0) {
          setStrategies(data);
          const param = searchParams.get('strategy');
          const chosen = param && data.some((s) => s.id === param)
            ? param
            : data.some((s) => s.id === strategyId) ? strategyId : data[0].id;
          setStrategyId(chosen);
          const primary = data.find((s) => s.id === chosen)?.primary_timeframe;
          if (primary) setTimeframe(primary);
        }
      })
      .catch((err) => {
        logger.warn('Backtest', 'Failed to fetch strategies', err);
        setStrategiesError(true);
      });
  };
  useEffect(() => { fetchStrategies(); }, []);

  const handleRunBacktest = async () => {
    setSubmitting(true);
    setRunError(null);
    try {
      await fetchApi<{ task_id: string }>('/api/backtest/run', {
        method: 'POST',
        body: JSON.stringify({
          strategy_id: strategyId,
          start_date: startDate,
          end_date: endDate,
          initial_capital: Number(initialCapital),
          name: backtestName,
          timeframe,
        }),
      });
      toast('info', 'Backtest started');
      void jobs.refetch();
    } catch (err) {
      logger.error('Backtest', 'Failed to start backtest', err);
      setRunError(err);
      toast('error', describeApiError(err, 'Failed to start backtest'));
    } finally {
      setSubmitting(false);
    }
  };

  const handleStrategyChange = (id: string) => {
    setStrategyId(id);
    const primary = strategies.find((s) => s.id === id)?.primary_timeframe;
    if (primary) setTimeframe(primary);
  };

  // Auto-run from Save & Backtest only once the strategy list has applied its primary timeframe
  useEffect(() => {
    if (autorunTriggered.current) return;
    if (searchParams.get('autorun') !== 'true') return;
    const requested = searchParams.get('strategy');
    if (!requested || strategyId !== requested || submitting) return;
    if (!strategies.some((s) => s.id === requested)) return;
    autorunTriggered.current = true;
    handleRunBacktest();
  }, [strategyId, strategies]);

  const handleDelete = async (id: string) => {
    try {
      await fetchApi<undefined>(`/api/backtest/results/${id}`, { method: 'DELETE' });
      toast('success', 'Backtest deleted');
      setBacktestRuns((prev) => prev.filter((r) => r.id !== id));
      setCompareIds((prev) => prev.filter((c) => c !== id));
    } catch (err) {
      logger.error('Backtest', 'Failed to delete backtest', err);
      toast('error', 'Failed to delete backtest');
    }
  };

  const toggleCompare = (id: string) => {
    setCompareIds((prev) =>
      prev.includes(id) ? prev.filter((c) => c !== id) : prev.length < 2 ? [...prev, id] : prev,
    );
  };

  const compareSelected = () => {
    const [a, b] = compareIds;
    if (!a || !b) return;
    router.push(`/backtest/compare?a=${a}&b=${b}`);
  };

  const columns = [
    {
      key: 'compare',
      header: 'Compare',
      render: (row: BacktestRun) => (
        <input
          type="checkbox"
          checked={compareIds.includes(row.id)}
          onChange={() => toggleCompare(row.id)}
          aria-label={`Compare ${row.name || row.strategy}`}
          className="h-5 w-5 cursor-pointer accent-accent"
        />
      ),
    },
    {
      key: 'name',
      header: 'Name',
      render: (row: BacktestRun) => (
        <span className="text-ink-1">{row.name || row.strategy}</span>
      ),
    },
    { key: 'strategy', header: 'Strategy', hideOnMobile: true },
    { key: 'period', header: 'Period', hideOnMobile: true },
    { key: 'totalTrades', header: 'Trades', numeric: true, render: (row: BacktestRun) => <span>{row.totalTrades}</span> },
    { key: 'winRate', header: 'Win rate', numeric: true, render: (row: BacktestRun) => metric(row.winRate) },
    {
      key: 'pnl',
      header: 'PnL',
      numeric: true,
      render: (row: BacktestRun) => (row.pnl === null ? metric(null) : <span className={row.pnlNegative ? 'font-medium text-danger' : 'font-medium'}>{row.pnl}</span>),
    },
    { key: 'maxDrawdown', header: 'Max DD', numeric: true, hideOnMobile: true, render: (row: BacktestRun) => metric(row.maxDrawdown) },
    { key: 'sharpe', header: 'Sharpe', numeric: true, hideOnMobile: true, render: (row: BacktestRun) => metric(row.sharpe) },
    {
      key: 'data',
      header: 'Data',
      render: (row: BacktestRun) => <ProvenanceBadge provenance={row.provenance} />,
    },
    {
      key: 'status',
      header: 'Status',
      render: (row: BacktestRun) => (
        <StatusBadge status={row.status} variant={row.statusVariant} size="sm" />
      ),
    },
    {
      key: 'actions',
      header: 'Actions',
      render: (row: BacktestRun) => (
        <div className="flex items-center gap-2">
          {row.status === 'Completed' && (
            <Link href={`/backtest/${row.id}`} className="inline-flex min-h-8 items-center text-xs text-ink-1 underline underline-offset-2">
              View
            </Link>
          )}
          <button
            aria-label={`Delete backtest ${row.name || row.strategy}`}
            onClick={() => setDeletingId(row.id)}
            className="inline-flex h-8 w-8 items-center justify-center text-ink-2 hover:text-danger transition-colors"
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
      ),
    },
  ];

  const noBars = runError !== null ? noStoredBars(runError) : null;

  return (
    <div className="flex flex-col gap-6">
      <ResearchNav current="/backtest" />
      <h1 className="text-xl font-semibold text-ink-1">Backtests</h1>
      {/* Runner form */}
      <Section title="Run Backtest">
        {strategiesError && (
          <div className="mb-4">
            <ErrorCard message="Failed to load strategies" onRetry={fetchStrategies} />
          </div>
        )}
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-6">
          <Select
            label="Strategy"
            options={strategies.map((s) => ({ value: s.id, label: s.name }))}
            value={strategyId}
            onChange={(e) => handleStrategyChange(e.target.value)}
            placeholder="Select strategy..."
          />
          <Select
            label="Timeframe"
            options={TIMEFRAME_OPTIONS}
            value={timeframe}
            onChange={(e) => setTimeframe(e.target.value)}
          />
          <Input label="Start Date" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <Input label="End Date" type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          <Input label="Initial Capital ($)" type="number" value={initialCapital} onChange={(e) => setInitialCapital(e.target.value)} />
          <Input label="Name (optional)" type="text" value={backtestName} onChange={(e) => setBacktestName(e.target.value)} placeholder="e.g. Q1 BTC 1h" />
        </div>
        {noBars ? (
          <div className="mt-4 flex flex-col gap-2" data-testid="run-error">
            <DataStateBadge state="no_data" />
            <ErrorCard message={noBars.title} />
            <p className="text-sm text-ink-2">{noBars.line}</p>
          </div>
        ) : runError !== null ? (
          <div className="mt-4" data-testid="run-error">
            <DataStateErrorCard error={runError} fallback="Failed to start backtest" />
          </div>
        ) : null}
        <div className="mt-4 flex flex-wrap gap-3">
          <Button variant="primary" onClick={handleRunBacktest} disabled={submitting}>
            <Play className="h-4 w-4" />
            {submitting ? 'Starting…' : 'Run Backtest'}
          </Button>
          <Button
            variant="outline"
            disabled={backtestRuns.length === 0}
            onClick={() => {
              const blob = new Blob([JSON.stringify(backtestRuns, null, 2)], { type: 'application/json' });
              const url = URL.createObjectURL(blob);
              const a = document.createElement('a');
              a.href = url;
              a.download = `backtest-results-${new Date().toISOString().slice(0, 10)}.json`;
              a.click();
              URL.revokeObjectURL(url);
            }}
          >
            <Download className="h-4 w-4" />
            Export Results
          </Button>
        </div>
      </Section>

      {/* Durable job queue */}
      <Section title="Jobs">
        {jobs.data ? (
          jobs.data.length === 0 ? (
            <p className="text-sm text-ink-2">
              No backtest jobs yet. Runs submitted here stay in this list across restarts.
            </p>
          ) : (
            <div className="flex flex-col gap-3">
              {jobs.data.map((job) => (
                <JobRow
                  key={job.task_id}
                  kind="backtest"
                  job={job}
                  onRefetch={() => void jobs.refetch()}
                />
              ))}
            </div>
          )
        ) : jobs.state === 'loading' ? (
          <p className="text-sm text-ink-2">Loading…</p>
        ) : (
          <DataStateErrorCard
            error={jobs.error}
            fallback="Jobs could not be loaded"
            onRetry={() => void jobs.refetch()}
          />
        )}
      </Section>

      {/* Results table */}
      <Section
        title="Backtest History"
        action={
          <Button
            variant="outline"
            disabled={compareIds.length !== 2}
            onClick={compareSelected}
          >
            Compare 2 selected
          </Button>
        }
      >
        {resultsError !== null ? (
          <DataStateErrorCard error={resultsError} fallback="Failed to load backtest results" onRetry={fetchResults} />
        ) : (
        <Table
          columns={columns}
          data={backtestRuns}
          keyExtractor={(row) => row.id}
          loading={loading}
        />
        )}
      </Section>

      <ConfirmDialog
        open={deletingId !== null}
        destructive
        title="Delete this backtest?"
        primaryLabel="Delete backtest"
        secondaryLabel="Keep it"
        pendingLabel="Deleting…"
        pendingHint="Closing this won't undo the delete."
        onConfirm={async () => {
          if (deletingId) {
            await handleDelete(deletingId);
            setDeletingId(null);
          }
        }}
        onClose={() => setDeletingId(null)}
      >
        <p>The result and its trades are removed from the database. This cannot be undone.</p>
      </ConfirmDialog>
    </div>
  );
}

export default function BacktestPage() {
  return (
    <Suspense>
      <BacktestPageContent />
    </Suspense>
  );
}
