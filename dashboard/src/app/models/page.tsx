'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowUpCircle, Brain, RotateCcw, Upload } from 'lucide-react';
import { DataCard } from '@/components/ui/DataCard';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { Button } from '@/components/ui/Button';
import { ErrorCard } from '@/components/ui/ErrorCard';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { TypedConfirm } from '@/components/ui/TypedConfirm';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { ApiError, apiHeaders, fetchApi } from '@/lib/api';
import { describeApiError, parseErrorDetail } from '@/lib/apiState';
import { useToast } from '@/components/ui/Toast';
import { pct, signed, utc } from '@/lib/format';
import { logger } from '@/lib/logger';

/* ---------- Types (the registry-backed routes' own shapes) ---------- */

interface ApiModel {
  id: string;
  name: string;
  version: number;
  stage: string; // Staging | Production | Archived
  model_type: string;
  metrics: {
    folds?: number | null;
    oos_accuracy?: number | null;
    oos_base_rate?: number | null;
    oos_mean_signed_return?: number | null;
    [key: string]: unknown;
  };
  feature_spec: Record<string, unknown>;
  drift: number | null;
  drift_status: 'unknown' | 'low' | 'moderate' | 'high';
  artifact_file: string;
  artifact_sha256: string;
  trained_at: string;
  created_by: string;
  training_job_id: string | null;
  archived_at: string | null;
  file_size: number;
  file_name: string;
}

interface StrategyOption {
  id: string;
  name: string;
  primary_timeframe?: string | null;
}

interface RetrainStatus {
  task_id: string;
  status: string; // queued | running | completed | failed | cancelled
  progress: number;
  error: string | null;
  result: { name?: string; version?: number; stage?: string } | null;
  metrics: Record<string, unknown> | null;
  provenance: Record<string, unknown> | null;
}

type Dialog =
  | { kind: 'promote'; model: ApiModel }
  | { kind: 'rollback'; model: ApiModel }
  | { kind: 'retrain'; name: string }
  | null;

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) });

/* ---------- Helpers ---------- */

function stageToVariant(stage: string): 'success' | 'info' | 'neutral' {
  switch (stage) {
    case 'Production': return 'success';
    case 'Staging': return 'info';
    default: return 'neutral';
  }
}

/** The drift badge's bands are the shared 0.2/0.25 thresholds the backend applies. */
function driftToVariant(ds: ApiModel['drift_status']): 'success' | 'warning' | 'error' | 'neutral' {
  switch (ds) {
    case 'low': return 'success';
    case 'moderate': return 'warning';
    case 'high': return 'error';
    default: return 'neutral';
  }
}

const driftLabels: Record<ApiModel['drift_status'], string> = {
  low: 'Low',
  moderate: 'Moderate',
  high: 'High',
  unknown: 'Unknown',
};

function jobStatusVariant(status: string): 'success' | 'warning' | 'info' | 'error' | 'neutral' {
  switch (status) {
    case 'completed': return 'success';
    case 'running': return 'info';
    case 'queued': return 'info';
    case 'failed': return 'error';
    default: return 'neutral';
  }
}

function fmtFileSize(bytes: number): string {
  if (bytes === 0) return '\u2014';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** An honest fraction metric as a percent, or null when the metric is absent. */
function fraction(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function metricCell(
  metrics: ApiModel['metrics'],
  key: 'oos_accuracy' | 'oos_base_rate',
): string {
  const value = fraction(metrics[key]);
  return pct(value == null ? null : value * 100) ?? 'N/A';
}

function signedReturnCell(metrics: ApiModel['metrics']): string {
  const value = fraction(metrics.oos_mean_signed_return);
  if (value == null) return 'N/A';
  const text = signed(value * 100);
  return text == null ? 'N/A' : `${text}%`;
}

/* ---------- Page ---------- */

export default function ModelsPage() {
  const { toast } = useToast();
  useEffect(() => { logger.info('Models', 'Page mounted'); }, []);
  const [models, setModels] = useState<ApiModel[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState(false);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  /* the retrain form */
  const [strategies, setStrategies] = useState<StrategyOption[]>([]);
  const [strategyId, setStrategyId] = useState('');
  const [symbol, setSymbol] = useState('');

  /* the running or finished ml_train job's status */
  const [taskId, setTaskId] = useState<string | null>(null);
  const [task, setTask] = useState<RetrainStatus | null>(null);
  const [taskError, setTaskError] = useState<unknown>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setFetchError(false);
    await fetchApi<ApiModel[]>('/api/models')
      .then((data) => setModels(data))
      .catch(() => { setFetchError(true); })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 30_000);
    return () => clearInterval(interval);
  }, [loadData]);

  useEffect(() => {
    fetchApi<StrategyOption[]>('/api/backtest/strategies')
      .then((data) => {
        setStrategies(data);
        if (data.length > 0) setStrategyId((current) => current || data[0].id);
      })
      .catch((err) => logger.warn('Models', 'Failed to fetch strategies', err));
  }, []);

  const pollTask = useCallback(async (id: string) => {
    try {
      const status = await fetchApi<RetrainStatus>(`/api/models/retrain/${id}`);
      setTask(status);
      setTaskError(null);
      if (TERMINAL.has(status.status)) await loadData();
    } catch (err) {
      logger.warn('Models', 'Failed to read the retrain job', err);
      setTaskError(err);
    }
  }, [loadData]);

  useEffect(() => {
    if (!taskId) { setTask(null); setTaskError(null); return; }
    void pollTask(taskId);
  }, [taskId, pollTask]);

  useEffect(() => {
    if (!taskId || !task || TERMINAL.has(task.status)) return;
    const timer = setInterval(() => void pollTask(taskId), 2000);
    return () => clearInterval(timer);
  }, [taskId, task, pollTask]);

  const closeDialog = () => { setDialog(null); setDialogError(null); };

  const attempt = async (model: ApiModel, action: 'promote' | 'rollback', reason: string): Promise<void> => {
    try {
      await fetchApi(`/api/models/${model.id}/${action}`, { method: 'POST', ...json({ reason }) });
      toast('success', action === 'promote'
        ? `${model.name} v${model.version} promoted to Production`
        : `${model.name} rolled back`);
      closeDialog();
      await loadData();
    } catch (err) {
      logger.error('Models', `Failed to ${action} ${model.name}`, err);
      setDialogError(describeApiError(err, `Failed to ${action} ${model.name}`));
    }
  };

  const submitRetrain = async (): Promise<void> => {
    if (!dialog || dialog.kind !== 'retrain') return;
    if (!strategyId || !symbol.trim()) {
      setDialogError('Pick a strategy and a symbol to train on');
      return;
    }
    try {
      const queued = await fetchApi<{ task_id: string }>(`/api/models/${dialog.name}/retrain`, {
        method: 'POST',
        ...json({ strategy_id: strategyId, symbol: symbol.trim() }),
      });
      toast('success', `Retrain of ${dialog.name} queued`);
      setSymbol('');
      closeDialog();
      setTaskId(queued.task_id);
    } catch (err) {
      logger.error('Models', 'Failed to queue the retrain', err);
      setDialogError(describeApiError(err, 'Failed to queue the retrain'));
    }
  };

  const postUpload = async (file: File): Promise<void> => {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || ''}/api/models/upload`, {
      method: 'POST',
      body: formData,
      credentials: 'include',
      headers: apiHeaders('POST'),
    });
    if (!res.ok) throw new ApiError(res.status, await res.text());
  };

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      await postUpload(file);
      toast('success', `Model "${file.name}" uploaded as the next Staging version`);
      loadData();
    } catch (err) {
      logger.error('Models', 'Failed to upload model', err);
      const detail = parseErrorDetail(err);
      toast('error', detail?.message ?? describeApiError(err, 'Upload failed'));
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const columns = [
    {
      key: 'name',
      header: 'Model',
      render: (row: ApiModel) => (
        <div className="flex items-center gap-2">
          <Brain className="h-4 w-4 text-ink-2 shrink-0" aria-hidden="true" />
          <div>
            <p className="text-sm font-medium text-ink-1">{row.name}</p>
            <p className="text-xs text-ink-2">v{row.version} · {fmtFileSize(row.file_size)}</p>
          </div>
        </div>
      ),
    },
    {
      key: 'status',
      header: 'Stage',
      render: (row: ApiModel) => (
        <StatusBadge status={row.stage} variant={stageToVariant(row.stage)} size="sm" />
      ),
    },
    {
      key: 'trained',
      header: 'Trained',
      hideOnMobile: true,
      render: (row: ApiModel) => <span className="text-sm">{utc(row.trained_at) ?? '—'}</span>,
    },
    {
      key: 'createdBy',
      header: 'Created by',
      hideOnMobile: true,
      render: (row: ApiModel) => (
        <span className="font-mono text-xs text-ink-2">{row.created_by}</span>
      ),
    },
    {
      key: 'job',
      header: 'Job',
      hideOnMobile: true,
      render: (row: ApiModel) => (
        row.training_job_id ? (
          <button
            type="button"
            className="font-mono text-xs text-accent underline-offset-2 hover:underline"
            onClick={() => setTaskId(row.training_job_id)}
            aria-label={`Show the training job of ${row.name} v${row.version}`}
          >
            {row.training_job_id.slice(0, 12)}
          </button>
        ) : (
          <span className="text-sm text-ink-2">{'\u2014'}</span>
        )
      ),
    },
    { key: 'accuracy', header: 'OOS Acc', numeric: true, render: (row: ApiModel) => metricCell(row.metrics, 'oos_accuracy') },
    { key: 'baseRate', header: 'Base Rate', numeric: true, hideOnMobile: true, render: (row: ApiModel) => metricCell(row.metrics, 'oos_base_rate') },
    { key: 'signedReturn', header: 'Signed Ret', numeric: true, hideOnMobile: true, render: (row: ApiModel) => signedReturnCell(row.metrics) },
    {
      key: 'drift',
      header: 'Drift',
      render: (row: ApiModel) => (
        <div className="flex items-center gap-2">
          <span className="text-sm text-ink-1">{row.drift != null ? row.drift.toFixed(3) : '\u2014'}</span>
          <StatusBadge status={driftLabels[row.drift_status]} variant={driftToVariant(row.drift_status)} size="sm" />
        </div>
      ),
    },
    {
      key: 'actions',
      header: 'Actions',
      render: (row: ApiModel) => (
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => { setDialogError(null); setDialog({ kind: 'promote', model: row }); }}
              disabled={row.stage === 'Production'}
            >
              <ArrowUpCircle className="h-3.5 w-3.5" />
              <span className="hidden lg:inline">Promote</span>
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => { setDialogError(null); setDialog({ kind: 'rollback', model: row }); }}
            >
              <RotateCcw className="h-3.5 w-3.5" />
              <span className="hidden lg:inline">Rollback</span>
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => { setDialogError(null); setSymbol(''); setDialog({ kind: 'retrain', name: row.name }); }}
            >
              <span className="hidden lg:inline">Retrain</span>
            </Button>
          </div>
      ),
    },
  ];

  const retrainName = dialog?.kind === 'retrain' ? dialog.name : null;

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-semibold text-ink-1">Models</h1>
      <p role="note" className="rounded-lg border border-line bg-surface p-3 text-sm text-ink-1">
        Production models are consumed: the ML overlay gates entry signals of strategies carrying
        an <span className="font-mono text-xs">ml_overlay</span> block and the ML-native strategy
        trades its predictions. Promoting, rolling back and retraining are human actions — the
        drift badge reads the trader&apos;s startup measurement and the HydraMLDriftHigh alert
        pages you when it is high; nothing retrains on its own.
      </p>

      {/* Header with upload */}
      <div className="flex items-center justify-between">
        <div />
        <div>
          <input
            ref={fileInputRef}
            type="file"
            accept=".onnx"
            aria-label="Upload ONNX model file"
            className="hidden"
            onChange={handleUpload}
          />
          <Button
            variant="primary"
            onClick={() => fileInputRef.current?.click()}
            loading={uploading}
          >
            <Upload className="h-4 w-4" />
            Upload Model
          </Button>
        </div>
      </div>

      {/* Retrain job status */}
      {taskId && (
        <DataCard title="Training job" description="The ml_train job this page queued or a version's own job">
          {taskError !== null && task === null ? (
            <ErrorCard message="Failed to read the training job" onRetry={() => void pollTask(taskId)} />
          ) : task === null ? (
            <p className="text-sm text-ink-2">Reading job {taskId}…</p>
          ) : (
            <div className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                <StatusBadge status={task.status} variant={jobStatusVariant(task.status)} size="sm" />
                <span className="font-mono text-xs text-ink-2">{task.task_id}</span>
                <span className="text-ink-2">{Math.round(task.progress)}%</span>
                {task.result?.name != null && task.result?.version != null && (
                  <span className="text-ink-1">
                    registered {task.result.name} v{task.result.version}
                    {task.result.stage ? ` (${task.result.stage === 'staging' ? 'Staging' : task.result.stage})` : ''}
                  </span>
                )}
                <Button variant="ghost" size="sm" onClick={() => setTaskId(null)}>Dismiss</Button>
              </div>
              {task.error && <p className="text-sm text-danger" role="alert">{task.error}</p>}
              {task.status === 'completed' && task.metrics && (
                <p className="text-sm text-ink-2">
                  OOS accuracy {metricCell(task.metrics as ApiModel['metrics'], 'oos_accuracy')}
                  {' · '}base rate {metricCell(task.metrics as ApiModel['metrics'], 'oos_base_rate')}
                  {' · '}signed return {signedReturnCell(task.metrics as ApiModel['metrics'])}
                </p>
              )}
            </div>
          )}
        </DataCard>
      )}

      {/* Model registry table */}
      <DataCard title="Model Registry" description="Every immutable version the registry holds">
        {fetchError ? (
          <ErrorCard message="Failed to load models" onRetry={loadData} />
        ) : loading ? (
          <div className="space-y-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-12 animate-pulse rounded-lg bg-raised" />
            ))}
          </div>
        ) : models && models.length > 0 ? (
          <Table
            columns={columns}
            data={models}
            keyExtractor={(row) => row.id}
          />
        ) : (
          <p className="text-sm text-ink-2 text-center py-8">No models yet</p>
        )}
      </DataCard>

      {/* Promote */}
      <TypedConfirm
        open={dialog?.kind === 'promote'}
        title={`Promote ${dialog?.kind === 'promote' ? `${dialog.model.name} v${dialog.model.version}` : ''} to Production?`}
        token="PROMOTE"
        primaryLabel="Promote to Production"
        secondaryLabel="Keep it"
        pendingLabel="Promoting…"
        error={dialog?.kind === 'promote' ? dialogError : null}
        onClose={closeDialog}
        onConfirm={(reason) =>
          dialog?.kind === 'promote'
            ? attempt(dialog.model, 'promote', reason)
            : Promise.resolve()
        }
      >
        {dialog?.kind === 'promote' && (
          <>
            <p>
              The current Production of {dialog.model.name} is archived with the demotion time;
              running sessions keep the model they pinned.
            </p>
            <p>
              OOS accuracy {metricCell(dialog.model.metrics, 'oos_accuracy')} against a base rate of{' '}
              {metricCell(dialog.model.metrics, 'oos_base_rate')} over{' '}
              {fraction(dialog.model.metrics.folds) ?? 0} folds.
            </p>
          </>
        )}
      </TypedConfirm>

      {/* Rollback */}
      <TypedConfirm
        open={dialog?.kind === 'rollback'}
        title={`Roll back ${dialog?.kind === 'rollback' ? dialog.model.name : ''}?`}
        token="ROLLBACK"
        primaryLabel="Roll back"
        secondaryLabel="Keep it"
        pendingLabel="Rolling back…"
        error={dialog?.kind === 'rollback' ? dialogError : null}
        onClose={closeDialog}
        onConfirm={(reason) =>
          dialog?.kind === 'rollback'
            ? attempt(dialog.model, 'rollback', reason)
            : Promise.resolve()
        }
      >
        <p>
          Rollback restores the most recently archived Production of{' '}
          {dialog?.kind === 'rollback' ? dialog.model.name : ''} and archives the current one. It
          refuses with nothing archived.
        </p>
      </TypedConfirm>

      {/* Retrain */}
      <ConfirmDialog
        open={dialog?.kind === 'retrain'}
        title={`Retrain ${retrainName ?? ''}`}
        primaryLabel="Queue retrain"
        secondaryLabel="Cancel"
        pendingLabel="Queueing…"
        pendingHint="Closing this won't cancel the job."
        onConfirm={() => submitRetrain()}
        onClose={closeDialog}
        primaryDisabled={!strategyId || !symbol.trim()}
      >
        <p>
          Queues a bounded ml_train job on the worker: it trains a challenger version of{' '}
          {retrainName} on the symbol&apos;s stored bars and registers it in Staging — promotion
          stays yours.
        </p>
        <Select
          label="Strategy"
          placeholder={strategies.length === 0 ? 'No strategies found' : 'Pick a strategy'}
          options={strategies.map((s) => ({ value: s.id, label: s.name }))}
          value={strategyId}
          onChange={(e) => setStrategyId(e.target.value)}
        />
        <Input
          label="Symbol"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value.toUpperCase())}
          placeholder="BTCUSDT"
          hint="Must be a member of the current top-100 universe."
          autoComplete="off"
        />
        {dialog?.kind === 'retrain' && dialogError && (
          <p role="alert" className="text-sm font-semibold text-danger">{dialogError}</p>
        )}
      </ConfirmDialog>
    </div>
  );
}
