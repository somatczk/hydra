'use client';

import { useCallback, useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { ArrowLeft, ArrowRight } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Section } from '@/components/ui/Section';
import { Table } from '@/components/ui/Table';
import { ApiError, fetchApi } from '@/lib/api';
import { DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { ProvenancePanel, type DataProvenance } from '@/components/backtest/DataProvenance';
import { describeApiError, stateFromError } from '@/lib/apiState';
import { logger } from '@/lib/logger';
import { pct, utc } from '@/lib/format';
import { usePoll } from '@/hooks/usePoll';
import { ResearchNav } from '@/components/research/ResearchNav';
import { JobRow } from '@/components/research/JobRow';
import { WalkForwardResults, type WalkForwardResult } from '@/components/research/WalkForwardResults';
import {
  holdoutLines,
  trialStatusText,
  utcDay,
  validationLines,
  type ResearchJob,
} from '@/lib/research';

/* ---------- Types ---------- */

interface TrialRecord {
  trial_number: number;
  params: Record<string, number>;
  sharpe: number;
  total_return: number;
  max_drawdown: number;
  total_trades: number;
  status: string;
  error?: string | null;
}

interface HyperoptProgress {
  task_id: string;
  status: string;
  strategy_id: string;
  created_at: string;
  completed_trials: number;
  total_trials: number;
  error?: string | null;
}

interface HyperoptResult {
  task_id: string;
  status: string;
  best_params: Record<string, number>;
  best_metric: number;
  trials: TrialRecord[];
  total_trials: number;
  completed_trials: number;
  n_trials: number;
  failed_trials: number;
  seed: number;
  train_start: string | null;
  train_end: string | null;
  holdout_start: string | null;
  holdout_end: string | null;
  holdout: { sharpe: number; total_return: number; max_drawdown: number; total_trades: number } | null;
  holdout_psr: number | null;
  dsr: number | null;
  pbo: number | null;
  provenance?: DataProvenance | null;
}

type JobsRow = ResearchJob;

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

const paramsText = (params: Record<string, number>) =>
  Object.entries(params)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(', ') || '—';

/* ---------- Shared pieces ---------- */

function BackLink({ onClick }: { onClick: () => void }) {
  return (
    <Button variant="ghost" size="sm" onClick={onClick}>
      <ArrowLeft className="h-4 w-4" />
    </Button>
  );
}

/** 404 reads "Run not found"; anything else renders the server's own reason. */
function MissingRun({ error, back }: { error: unknown; back: () => void }) {
  const missing = error === null || stateFromError(error) === 'not_found';
  return (
    <div className="flex flex-col items-center justify-center gap-4 py-20">
      {missing ? (
        <p className="text-sm text-ink-2">Run not found</p>
      ) : (
        <DataStateErrorCard error={error} fallback="Failed to load the run" />
      )}
      <Button variant="outline" onClick={back}>
        <ArrowLeft className="h-4 w-4" /> Back to Optimize
      </Button>
    </div>
  );
}

function RunHeader({
  title,
  strategy,
  startedAt,
  onBack,
  action,
}: {
  title: string;
  strategy: string | null | undefined;
  startedAt?: string | null;
  onBack: () => void;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-center gap-3">
        <BackLink onClick={onBack} />
        <div>
          <h1 className="text-lg font-semibold text-ink-1">{title}</h1>
          <p className="text-xs text-ink-2">
            {strategy ? <>Strategy: {strategy}</> : 'Strategy: —'}
            {startedAt ? <> &middot; started {utc(startedAt)}</> : null}
          </p>
        </div>
      </div>
      {action}
    </div>
  );
}

/* ---------- Hyperopt ---------- */

function HyperoptView({ taskId }: { taskId: string }) {
  const router = useRouter();
  const [progress, setProgress] = useState<HyperoptProgress | null>(null);
  const [result, setResult] = useState<HyperoptResult | null>(null);
  const [resultError, setResultError] = useState<unknown>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [polling, setPolling] = useState(true);
  const [refresh, setRefresh] = useState(0);

  const poll = useCallback(async () => {
    try {
      const p = await fetchApi<HyperoptProgress>(`/api/backtest/hyperopt/${taskId}`);
      setProgress(p);
      setLoadError(null);
      if (TERMINAL.has(p.status)) {
        setPolling(false);
        if (p.status === 'completed') {
          try {
            setResult(await fetchApi<HyperoptResult>(`/api/backtest/hyperopt/${taskId}/results`));
          } catch (err) {
            logger.warn('OptimizeTask', 'Failed to load hyperopt results', err);
            setResultError(err);
          }
        }
      }
    } catch (err) {
      logger.warn('OptimizeTask', 'Failed to load hyperopt run', err);
      setLoadError(err);
      setPolling(false);
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    void poll();
  }, [poll, refresh]);

  useEffect(() => {
    if (!polling) return;
    const timer = setInterval(() => void poll(), 2000);
    return () => clearInterval(timer);
  }, [poll, polling]);

  if (loading && progress === null) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-ink-2">Loading optimisation run…</p>
      </div>
    );
  }

  if (progress === null) {
    return <MissingRun error={loadError} back={() => router.push('/optimize')} />;
  }

  const job: ResearchJob = {
    task_id: progress.task_id,
    status: progress.status,
    created_at: progress.created_at,
    strategy_id: progress.strategy_id,
    error: progress.error ?? null,
    completed_trials: progress.completed_trials,
    total_trials: progress.total_trials,
  };

  const holdout = result ? holdoutLines(result.holdout, result) : null;
  const validation = result ? validationLines(result) : null;
  const trainWindow =
    result && result.train_start && result.train_end
      ? `Train window: ${utcDay(result.train_start)} to ${utcDay(result.train_end)}`
      : null;

  const trialColumns = [
    {
      key: 'n',
      header: '#',
      render: (t: TrialRecord) => <span className="text-ink-2">{t.trial_number}</span>,
    },
    {
      key: 'params',
      header: 'Parameters',
      render: (t: TrialRecord) => (
        <span className="font-mono text-xs text-ink-2">{paramsText(t.params)}</span>
      ),
    },
    {
      key: 'sharpe',
      header: 'Sharpe',
      numeric: true,
      render: (t: TrialRecord) => (
        <span className="font-medium">{t.status === 'ok' ? t.sharpe.toFixed(2) : '—'}</span>
      ),
    },
    {
      key: 'return',
      header: 'Return',
      numeric: true,
      hideOnMobile: true,
      render: (t: TrialRecord) => (
        <span>{t.status === 'ok' ? pct(t.total_return * 100) ?? '—' : '—'}</span>
      ),
    },
    {
      key: 'dd',
      header: 'Max DD',
      numeric: true,
      hideOnMobile: true,
      render: (t: TrialRecord) => (
        <span className="text-danger">
          {t.status === 'ok' ? pct(t.max_drawdown * 100) ?? '—' : '—'}
        </span>
      ),
    },
    {
      key: 'trades',
      header: 'Trades',
      numeric: true,
      hideOnMobile: true,
      render: (t: TrialRecord) => <span>{t.status === 'failed' ? '—' : t.total_trades}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      render: (t: TrialRecord) => {
        const label = trialStatusText(t.status);
        if (label === null) return <span className="text-ink-2">{'\u2014'}</span>;
        if (t.status === 'failed') {
          return (
            <span className="text-danger">
              Failed{t.error ? `: ${t.error}` : ''}
            </span>
          );
        }
        return <span className="text-ink-2">No trades</span>;
      },
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <ResearchNav current="/optimize" />
      <RunHeader
        title="Hyperopt run"
        strategy={progress.strategy_id}
        startedAt={progress.created_at}
        onBack={() => router.push('/optimize')}
        action={
          progress.strategy_id ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => router.push(`/builder?strategy=${progress.strategy_id}`)}
            >
              <ArrowRight className="h-4 w-4" /> Open in Builder
            </Button>
          ) : undefined
        }
      />
      {loadError !== null && (
        <DataStateErrorCard
          error={loadError}
          fallback="Lost contact with the run"
          onRetry={() => setRefresh((n) => n + 1)}
        />
      )}
      {result !== null ? (
        <>
          <div className="rounded-lg border border-line bg-surface px-4 py-3" data-testid="run-summary">
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
              <span className="font-medium text-ink-1">
                Best Sharpe {result.best_metric.toFixed(2)}
              </span>
              <span className="font-mono text-ink-1">Best params {paramsText(result.best_params)}</span>
              <span className="text-ink-2">
                {result.completed_trials} completed · {result.failed_trials} failed
              </span>
              <span className="text-ink-2">Seed {result.seed}</span>
              {trainWindow && <span className="text-ink-2">{trainWindow}</span>}
              {validation && <span className="text-ink-2">{validation}</span>}
            </div>
          </div>
          {holdout && (
            <div className="rounded-lg border border-line bg-surface px-4 py-3" data-testid="holdout">
              <p className="mb-2 text-xs font-medium text-ink-2">Holdout</p>
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
                {holdout.map((line) => (
                  <span key={line} className="text-ink-1">
                    {line}
                  </span>
                ))}
              </div>
            </div>
          )}
          {result.provenance && <ProvenancePanel provenance={result.provenance} />}
          <Section title="Trials" action={<span className="text-xs text-ink-2">{result.trials.length} recorded</span>}>
            <Table
              columns={trialColumns}
              data={result.trials}
              keyExtractor={(t) => String(t.trial_number)}
              emptyMessage="No trials recorded"
            />
          </Section>
        </>
      ) : resultError !== null ? (
        <DataStateErrorCard
          error={resultError}
          fallback="Failed to load results"
          onRetry={() => setRefresh((n) => n + 1)}
        />
      ) : progress.status === 'completed' ? (
        <p className="text-sm text-ink-2">Loading results…</p>
      ) : (
        <JobRow
          kind="hyperopt"
          job={job}
          onRefetch={() => {
            setPolling(true);
            setRefresh((n) => n + 1);
          }}
        />
      )}
    </div>
  );
}

/* ---------- Walk-forward ---------- */

function WalkforwardView({ taskId }: { taskId: string }) {
  const router = useRouter();
  const [result, setResult] = useState<WalkForwardResult | null>(null);
  const [notCompleted, setNotCompleted] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  const jobs = usePoll<JobsRow[]>('/api/backtest/jobs?kind=walkforward&limit=50', 10000);
  const row = jobs.data?.find((j) => j.task_id === taskId) ?? null;

  const loadResults = useCallback(async () => {
    try {
      const r = await fetchApi<WalkForwardResult>(`/api/backtest/walkforward/${taskId}/results`);
      setResult(r);
      setNotCompleted(null);
      setLoadError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNotCompleted(describeApiError(err, 'Run not completed'));
      } else {
        logger.warn('OptimizeTask', 'Failed to load walk-forward results', err);
        setLoadError(err);
      }
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    void loadResults();
  }, [loadResults]);

  // the durable row reaches completed: read the folds (skipped while the first read is in flight)
  useEffect(() => {
    if (!loading && row?.status === 'completed' && result === null && loadError === null) {
      void loadResults();
    }
  }, [loading, row?.status, result, loadError, loadResults]);

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-ink-2">Loading optimisation run…</p>
      </div>
    );
  }

  if (loadError !== null && result === null) {
    return <MissingRun error={loadError} back={() => router.push('/optimize')} />;
  }

  return (
    <div className="flex flex-col gap-6">
      <ResearchNav current="/optimize" />
      <RunHeader
        title="Walk-forward run"
        strategy={row?.strategy_id}
        startedAt={row?.created_at}
        onBack={() => router.push('/optimize')}
      />
      {result !== null ? (
        <WalkForwardResults result={result} />
      ) : (
        <>
          {row ? (
            <JobRow kind="walkforward" job={row} onRefetch={() => void jobs.refetch()} />
          ) : jobs.data === null && jobs.error !== null ? (
            <DataStateErrorCard
              error={jobs.error}
              fallback="Failed to read the run's job row"
              onRetry={() => void jobs.refetch()}
            />
          ) : (
            <p className="text-sm text-ink-2">Reading the run…</p>
          )}
          {notCompleted && (
            <p className="text-sm text-danger" data-testid="not-completed">
              {notCompleted}
            </p>
          )}
        </>
      )}
    </div>
  );
}

/* ---------- Page ---------- */

export default function OptimizeTaskPage() {
  const params = useParams();
  const router = useRouter();
  const taskId = params.taskId as string;

  if (taskId.startsWith('ho-')) return <HyperoptView taskId={taskId} />;
  if (taskId.startsWith('wf-')) return <WalkforwardView taskId={taskId} />;
  return <MissingRun error={null} back={() => router.push('/optimize')} />;
}
