'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Play } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Section } from '@/components/ui/Section';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { ErrorCard } from '@/components/ui/ErrorCard';
import { describeApiError, parseErrorDetail } from '@/lib/apiState';
import { logger } from '@/lib/logger';
import { usePoll } from '@/hooks/usePoll';
import { ResearchNav } from '@/components/research/ResearchNav';
import { JobRow } from '@/components/research/JobRow';
import { ParamSpaceEditor } from '@/components/research/ParamSpaceEditor';
import { WalkForwardResults, type WalkForwardResult } from '@/components/research/WalkForwardResults';
import {
  noStoredBars,
  utcDay,
  type JobKind,
  type ResearchJob,
  type StrategyParamDef,
  type WireParamDef,
} from '@/lib/research';

/* ---------- Types ---------- */

interface StrategyOption {
  id: string;
  name: string;
}

type JobsRow = ResearchJob & { kind: JobKind };

interface HyperoptProgress {
  task_id: string;
  status: string;
  strategy_id: string;
  created_at: string;
  completed_trials: number;
  total_trials: number;
  error?: string | null;
}

type Mode = 'hyperopt' | 'walkforward';

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

const METHOD_OPTIONS = [
  { value: 'bayesian', label: 'Bayesian' },
  { value: 'grid', label: 'Grid' },
];

/** C-WF: the 422 range_too_short envelope in operator words; null for anything else. */
function rangeTooShort(err: unknown, foldCount: number): { title: string; line: string } | null {
  const detail = parseErrorDetail(err);
  if (detail?.code !== 'range_too_short') return null;
  const first = detail.available_first_ts;
  const last = detail.available_last_ts;
  const line =
    typeof first === 'string' && typeof last === 'string'
      ? `Stored bars cover ${utcDay(first)} to ${utcDay(last)}.`
      : '';
  return { title: `The stored range is too short for ${foldCount} folds.`, line };
}

/* ---------- Page ---------- */

export default function OptimizePage() {
  const router = useRouter();
  const { toast } = useToast();

  const [mode, setMode] = useState<Mode>('hyperopt');
  const [strategies, setStrategies] = useState<StrategyOption[]>([]);
  const [strategyId, setStrategyId] = useState('');
  const [strategiesError, setStrategiesError] = useState<unknown>(null);
  const [strategiesReload, setStrategiesReload] = useState(0);

  const [defs, setDefs] = useState<StrategyParamDef[] | null>(null);
  const [paramsError, setParamsError] = useState<unknown>(null);
  const [paramSpace, setParamSpace] = useState<WireParamDef[] | null>(null);
  const [paramsReload, setParamsReload] = useState(0);

  const [startDate, setStartDate] = useState('2025-01-01');
  const [endDate, setEndDate] = useState('2026-03-01');
  const [initialCapital, setInitialCapital] = useState('10000');
  const [method, setMethod] = useState('bayesian');
  const [maxTrials, setMaxTrials] = useState('100');
  const [foldCount, setFoldCount] = useState('6');
  const [trainPct, setTrainPct] = useState('75');
  const [anchored, setAnchored] = useState(false);
  const [trialsPerFold, setTrialsPerFold] = useState('20');

  const [submitting, setSubmitting] = useState(false);
  const [runError, setRunError] = useState<unknown>(null);

  // the hyperopt run started here: watched on its own progress endpoint until terminal
  const [hoWatch, setHoWatch] = useState<string | null>(null);
  const [hoJob, setHoJob] = useState<ResearchJob | null>(null);
  const [hoWatchError, setHoWatchError] = useState<unknown>(null);
  // the id from the submit response, so the watcher can be re-armed after a lost first poll
  const hoStartedRef = useRef<string | null>(null);

  // the walk-forward run started here: watched through the durable jobs list
  const [wfWatchId, setWfWatchId] = useState<string | null>(null);
  const [wfResult, setWfResult] = useState<WalkForwardResult | null>(null);
  const [wfResultError, setWfResultError] = useState<unknown>(null);

  const hyperoptJobs = usePoll<JobsRow[]>('/api/backtest/jobs?kind=hyperopt&limit=50', 10000);
  const walkforwardJobs = usePoll<JobsRow[]>('/api/backtest/jobs?kind=walkforward&limit=50', 10000);

  useEffect(() => {
    setStrategiesError(null);
    fetchApi<StrategyOption[]>('/api/backtest/strategies')
      .then((data) => {
        setStrategies(data);
        setStrategyId((prev) => (prev && data.some((s) => s.id === prev) ? prev : data[0]?.id ?? ''));
      })
      .catch((err) => {
        logger.warn('Optimize', 'Failed to fetch strategies', err);
        setStrategiesError(err);
      });
  }, [strategiesReload]);

  useEffect(() => {
    if (!strategyId) {
      setDefs(null);
      setParamsError(null);
      setParamSpace(null);
      return;
    }
    setDefs(null);
    setParamsError(null);
    setParamSpace(null);
    // a slower previous strategy must not bind its param space to this one
    let cancelled = false;
    fetchApi<StrategyParamDef[]>(`/api/backtest/strategies/${strategyId}/params`)
      .then((data) => {
        if (cancelled) return;
        setDefs(data);
      })
      .catch((err) => {
        if (cancelled) return;
        logger.warn('Optimize', 'Failed to fetch strategy params', err);
        setParamsError(err);
      });
    return () => {
      cancelled = true;
    };
  }, [strategyId, paramsReload]);

  const handleParamSpace = useCallback((space: WireParamDef[] | null) => setParamSpace(space), []);

  const applyProgress = useCallback(
    (p: HyperoptProgress, isCancelled?: () => boolean) => {
      setHoJob({
        task_id: p.task_id,
        status: p.status,
        created_at: p.created_at,
        strategy_id: p.strategy_id,
        error: p.error ?? null,
        completed_trials: p.completed_trials,
        total_trials: p.total_trials,
      });
      if (TERMINAL.has(p.status)) {
        setHoWatch(null);
        if (p.status === 'completed' && !isCancelled?.()) {
          router.push(`/optimize/${p.task_id}`);
        }
      }
    },
    [router],
  );

  const pollHyperopt = useCallback(
    async (taskId: string, isCancelled?: () => boolean) => {
      try {
        applyProgress(await fetchApi<HyperoptProgress>(`/api/backtest/hyperopt/${taskId}`), isCancelled);
        setHoWatchError(null);
      } catch (err) {
        setHoWatchError(err);
        setHoWatch(null);
      }
    },
    [applyProgress],
  );

  useEffect(() => {
    if (hoWatch === null) return;
    let cancelled = false;
    const poll = () => void pollHyperopt(hoWatch, () => cancelled);
    poll();
    const timer = setInterval(poll, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [hoWatch, pollHyperopt]);

  const wfRow = useMemo(
    () => walkforwardJobs.data?.find((j) => j.task_id === wfWatchId) ?? null,
    [walkforwardJobs.data, wfWatchId],
  );

  // the durable row says completed: read the folds once and render them inline
  useEffect(() => {
    if (!wfWatchId || wfRow?.status !== 'completed' || wfResult || wfResultError) return;
    let cancelled = false;
    fetchApi<WalkForwardResult>(`/api/backtest/walkforward/${wfWatchId}/results`)
      .then((r) => {
        if (cancelled) return;
        setWfResult(r);
        setWfWatchId(null);
        toast('success', 'Walk-forward completed');
      })
      .catch((err) => {
        if (cancelled) return;
        logger.warn('Optimize', 'Failed to load walk-forward results', err);
        setWfResultError(err);
      });
    return () => {
      cancelled = true;
    };
  }, [wfWatchId, wfRow?.status, wfResult, wfResultError, toast]);

  const capital = Number(initialCapital);
  const trials = Number(maxTrials);
  const folds = Number(foldCount);
  const train = Number(trainPct);
  const perFold = Number(trialsPerFold);
  const canSubmit =
    !!strategyId &&
    !!startDate &&
    !!endDate &&
    paramSpace !== null &&
    paramSpace.length > 0 &&
    Number.isFinite(capital) &&
    capital > 0 &&
    (mode === 'hyperopt'
      ? Number.isFinite(trials)
      : Number.isFinite(folds) &&
        Number.isFinite(perFold) &&
        Number.isFinite(train) &&
        train > 0 &&
        train < 100);

  const startRun = async () => {
    if (!canSubmit || !paramSpace) return;
    setSubmitting(true);
    setRunError(null);
    setWfResult(null);
    setWfResultError(null);
    setHoJob(null);
    try {
      if (mode === 'hyperopt') {
        const res = await fetchApi<{ task_id: string }>('/api/backtest/hyperopt', {
          method: 'POST',
          body: JSON.stringify({
            strategy_id: strategyId,
            param_space: paramSpace,
            method,
            max_trials: trials,
            start_date: startDate,
            end_date: endDate,
            initial_capital: capital,
          }),
        });
        setHoWatch(res.task_id);
        hoStartedRef.current = res.task_id;
        void hyperoptJobs.refetch();
        toast('info', 'Hyperopt started');
      } else {
        const res = await fetchApi<{ task_id: string }>('/api/backtest/walkforward', {
          method: 'POST',
          body: JSON.stringify({
            strategy_id: strategyId,
            param_space: paramSpace,
            method,
            max_trials: perFold,
            fold_count: folds,
            train_pct: train / 100,
            anchored,
            start_date: startDate,
            end_date: endDate,
            initial_capital: capital,
          }),
        });
        setWfWatchId(res.task_id);
        void walkforwardJobs.refetch();
        toast('info', 'Walk-forward started');
      }
    } catch (err) {
      logger.error('Optimize', 'Failed to start the run', err);
      setRunError(err);
      toast('error', describeApiError(err, 'Failed to start the run'));
    } finally {
      setSubmitting(false);
    }
  };

  const recent = useMemo(
    () =>
      [...(hyperoptJobs.data ?? []), ...(walkforwardJobs.data ?? [])]
        .filter((j) => j.task_id !== hoWatch)
        .sort((a, b) => Date.parse(b.created_at ?? '') - Date.parse(a.created_at ?? '')),
    [hyperoptJobs.data, walkforwardJobs.data, hoWatch],
  );

  const refetchRuns = () => {
    void hyperoptJobs.refetch();
    void walkforwardJobs.refetch();
  };

  const noBars = runError !== null ? noStoredBars(runError) : null;
  const tooShort = runError !== null && mode === 'walkforward' ? rangeTooShort(runError, folds) : null;

  const jobsError = hyperoptJobs.data === null ? hyperoptJobs.error : null;
  const wfJobsError = walkforwardJobs.data === null ? walkforwardJobs.error : null;

  return (
    <div className="flex flex-col gap-6">
      <ResearchNav current="/optimize" />
      <div>
        <h1 className="text-xl font-semibold text-ink-1">Strategy optimiser</h1>
        <p className="mt-1 text-sm text-ink-2">
          Find optimal parameter combinations for your strategy
        </p>
      </div>

      {strategiesError !== null && (
        <DataStateErrorCard
          error={strategiesError}
          fallback="Failed to load strategies"
          onRetry={() => setStrategiesReload((n) => n + 1)}
        />
      )}

      <div
        role="group"
        aria-label="Optimisation mode"
        className="inline-flex w-fit rounded-lg border border-line bg-surface p-1"
      >
        {(['hyperopt', 'walkforward'] as Mode[]).map((m) => (
          <button
            key={m}
            type="button"
            aria-pressed={mode === m}
            onClick={() => setMode(m)}
            className={`h-10 max-lg:h-11 rounded-md px-4 text-sm font-medium transition-colors ${
              mode === m ? 'bg-raised text-ink-1' : 'text-ink-2 hover:text-ink-1'
            }`}
          >
            {m === 'hyperopt' ? 'Hyperopt' : 'Walk-forward'}
          </button>
        ))}
      </div>

      <Section title="Run">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          <Select
            label="Strategy"
            options={strategies.map((s) => ({ value: s.id, label: s.name }))}
            value={strategyId}
            onChange={(e) => setStrategyId(e.target.value)}
            placeholder="Select strategy…"
          />
          <Select
            label="Method"
            options={METHOD_OPTIONS}
            value={method}
            onChange={(e) => setMethod(e.target.value)}
          />
          <Input
            label="Initial capital (USDT)"
            type="number"
            min={0}
            value={initialCapital}
            onChange={(e) => setInitialCapital(e.target.value)}
          />
          <Input label="Start Date" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <Input label="End Date" type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          {mode === 'hyperopt' ? (
            <Input
              label="Max Trials"
              type="number"
              min={1}
              max={500}
              value={maxTrials}
              onChange={(e) => setMaxTrials(e.target.value)}
            />
          ) : (
            <>
              <Input
                label="Folds"
                type="number"
                min={2}
                max={24}
                value={foldCount}
                onChange={(e) => setFoldCount(e.target.value)}
              />
              <Input
                label="Train %"
                type="number"
                min={1}
                max={99}
                value={trainPct}
                onChange={(e) => setTrainPct(e.target.value)}
              />
              <Input
                label="Trials per fold"
                type="number"
                min={1}
                max={200}
                value={trialsPerFold}
                onChange={(e) => setTrialsPerFold(e.target.value)}
              />
              <label className="flex h-10 max-lg:h-11 items-center gap-2 text-sm text-ink-2">
                <input
                  type="checkbox"
                  checked={anchored}
                  onChange={(e) => setAnchored(e.target.checked)}
                  className="h-5 w-5 accent-accent"
                />
                Anchored
              </label>
            </>
          )}
        </div>

        <div className="mt-4">
          <p className="mb-2 text-xs font-medium text-ink-2">Parameter ranges</p>
          {paramsError !== null ? (
            <DataStateErrorCard
              error={paramsError}
              fallback="Failed to load strategy parameters"
              onRetry={() => setParamsReload((n) => n + 1)}
            />
          ) : (
            <ParamSpaceEditor defs={defs} onChange={handleParamSpace} disabled={submitting} />
          )}
        </div>

        {tooShort ? (
          <div className="mt-4 flex flex-col gap-2" data-testid="run-error">
            <DataStateBadge state="invalid" />
            <ErrorCard message={tooShort.title} />
            {tooShort.line && <p className="text-sm text-ink-2">{tooShort.line}</p>}
          </div>
        ) : noBars ? (
          <div className="mt-4 flex flex-col gap-2" data-testid="run-error">
            <DataStateBadge state="no_data" />
            <ErrorCard message={noBars.title} />
            <p className="text-sm text-ink-2">{noBars.line}</p>
          </div>
        ) : runError !== null ? (
          <div className="mt-4" data-testid="run-error">
            <DataStateErrorCard error={runError} fallback="Failed to start the run" />
          </div>
        ) : null}

        <div className="mt-4 flex flex-col gap-3">
          <Button variant="primary" onClick={startRun} disabled={!canSubmit || submitting}>
            <Play className="h-4 w-4" />
            {submitting ? 'Starting…' : mode === 'hyperopt' ? 'Run Hyperopt' : 'Run Walk-forward'}
          </Button>
          {hoJob && (
            <JobRow
              kind="hyperopt"
              job={hoJob}
              onRefetch={() => hoWatch !== null && void pollHyperopt(hoWatch)}
            />
          )}
          {hoWatchError !== null && (
            <DataStateErrorCard
              error={hoWatchError}
              fallback="Lost contact with the run"
              onRetry={() => hoStartedRef.current && setHoWatch(hoStartedRef.current)}
            />
          )}
          {wfResultError !== null && (
            <DataStateErrorCard
              error={wfResultError}
              fallback="Failed to load walk-forward results"
              onRetry={() => setWfResultError(null)}
            />
          )}
        </div>
      </Section>

      {wfResult && <WalkForwardResults result={wfResult} />}

      <Section title="Recent runs">
        {recent.length > 0 ? (
          <div className="flex flex-col gap-3">
            {recent.map((job) => (
              <JobRow key={job.task_id} kind={job.kind} job={job} onRefetch={refetchRuns} />
            ))}
          </div>
        ) : jobsError !== null || wfJobsError !== null ? (
          <DataStateErrorCard
            error={jobsError ?? wfJobsError}
            fallback="Failed to load runs"
            onRetry={refetchRuns}
          />
        ) : hyperoptJobs.state === 'loading' && walkforwardJobs.state === 'loading' ? (
          <p className="text-sm text-ink-2">Loading…</p>
        ) : (
          <p className="text-sm text-ink-2">
            No optimisation runs yet. Runs submitted here stay in this list across restarts.
          </p>
        )}
      </Section>
    </div>
  );
}
