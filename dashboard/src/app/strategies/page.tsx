'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Plus, Edit2, Trash2, BarChart3, Play, Eye, Download, Upload, LayoutTemplate } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { cn } from '@/components/ui/cn';
import { fetchApi, fetchApiText } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { describeApiError, stateFromError } from '@/lib/apiState';
import { usePoll } from '@/hooks/usePoll';
import { logger } from '@/lib/logger';
import { pct, signed, signedLabel, utc } from '@/lib/format';
import type { SessionResponse } from '@/lib/derive';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { ResearchNav } from '@/components/research/ResearchNav';
import { StartSessionDialog } from '@/components/sessions/StartSessionDialog';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { StrategyDriftPanel, type StrategyDrift } from '@/components/strategies/StrategyDriftPanel';

/* ---------- Types ---------- */

interface StrategyPerformance {
  total_pnl: number;
  win_rate: number;
  total_trades: number;
  sharpe_ratio: number | null;
  max_drawdown: number | null;
}

interface BuilderStrategy {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  filename: string;
  editable: boolean;
}

/* ---------- Helpers ---------- */

function sessionStatusLabel(session: SessionResponse | null): string {
  if (!session) return '';
  const mode = session.trading_mode === 'paper' ? 'Paper Running' : 'Live Running';
  const at = session.started_at ? utc(session.started_at) : null;
  return at ? `${mode} · ${at}` : mode;
}

function sessionStatusVariant(session: SessionResponse | null): 'success' | 'warning' | 'info' | 'neutral' {
  if (!session) return 'neutral';
  return session.trading_mode === 'paper' ? 'info' : 'warning';
}

function sharpeClass(v: number | null): string {
  if (v === null) return 'text-ink-2';
  return v >= 0 ? 'text-ink-1' : 'text-danger';
}

/* ---------- Page ---------- */

export default function StrategiesPage() {
  const router = useRouter();
  const { toast } = useToast();
  // The shell already polls the session list and system config; this page reads its context (A3-second).
  const { sessions, config } = useShellStatus();
  const list = usePoll<BuilderStrategy[]>('/api/strategies', 30_000);

  const [perfMap, setPerfMap] = useState<Record<string, StrategyPerformance>>({});
  const [perfErrors, setPerfErrors] = useState<Record<string, unknown>>({});
  const [driftMap, setDriftMap] = useState<Record<string, StrategyDrift>>({});
  const [driftErrors, setDriftErrors] = useState<Record<string, unknown>>({});
  const [deleteTarget, setDeleteTarget] = useState<BuilderStrategy | null>(null);
  const [startId, setStartId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Performance stays one call per strategy, as before; it does not wait on the session list.
  useEffect(() => {
    const strategies = list.data;
    if (strategies === null) return;
    let alive = true;
    const ids = strategies.map((s) => s.id);
    (async () => {
      const entries = await Promise.allSettled(
        ids.map((id) => fetchApi<StrategyPerformance>(`/api/strategies/${id}/performance`)),
      );
      if (!alive) return;
      const nextPerf: Record<string, StrategyPerformance> = {};
      const nextPerfErrors: Record<string, unknown> = {};
      entries.forEach((entry, i) => {
        if (entry.status === 'fulfilled') nextPerf[ids[i]] = entry.value;
        else nextPerfErrors[ids[i]] = entry.reason;
      });
      setPerfMap(nextPerf);
      setPerfErrors(nextPerfErrors);
    })();
    return () => {
      alive = false;
    };
  }, [list.data]);

  // E-4 (A4): the hot-reload detail is fetched only for strategies with a running
  // session (typically 0-3), never N of them; the session list says which ones run.
  useEffect(() => {
    const strategies = list.data;
    const running = sessions.data;
    if (strategies === null || running === null) return;
    let alive = true;
    const known = new Set(strategies.map((s) => s.id));
    const runningIds = [...new Set(running.filter((s) => s.status === 'running').map((s) => s.strategy_id))]
      .filter((id) => known.has(id))
      .sort();
    (async () => {
      const entries = await Promise.allSettled(runningIds.map((id) => fetchApi<StrategyDrift>(`/api/strategies/${id}`)));
      if (!alive) return;
      const nextDrift: Record<string, StrategyDrift> = {};
      const nextDriftErrors: Record<string, unknown> = {};
      entries.forEach((entry, i) => {
        if (entry.status === 'fulfilled') nextDrift[runningIds[i]] = entry.value;
        else nextDriftErrors[runningIds[i]] = entry.reason;
      });
      setDriftMap(nextDrift);
      setDriftErrors(nextDriftErrors);
    })();
    return () => {
      alive = false;
    };
  }, [list.data, sessions.data]);

  const refetchDrift = useCallback(async (id: string) => {
    try {
      const detail = await fetchApi<StrategyDrift>(`/api/strategies/${id}`);
      setDriftMap((prev) => ({ ...prev, [id]: detail }));
      setDriftErrors((prev) => Object.fromEntries(Object.entries(prev).filter(([key]) => key !== id)));
    } catch (err) {
      setDriftErrors((prev) => ({ ...prev, [id]: err }));
    }
  }, []);

  const tradingMode = config.data ? config.data.trading_mode : null;
  const strategies = list.data;
  const loading = list.state === 'loading';
  const loadError = list.error;

  const sessionFor = (id: string): SessionResponse | null =>
    (sessions.data ?? []).find((s) => s.strategy_id === id && s.status === 'running') ?? null;

  const handleToggle = async (id: string) => {
    try {
      await fetchApi(`/api/strategies/${id}/toggle`, { method: 'POST' });
      toast('success', 'Strategy toggled');
      void list.refetch();
    } catch (err) {
      logger.error('Strategies', 'Failed to toggle strategy', err);
      toast('error', describeApiError(err, 'Failed to toggle strategy'));
    }
  };

  const openDelete = (strategy: BuilderStrategy) => {
    // The no-session fact comes from the last 30 s poll; refresh it as the door opens.
    void sessions.refetch();
    setDeleteTarget(strategy);
  };

  const confirmDelete = async () => {
    if (deleteTarget === null) return;
    try {
      await fetchApi(`/api/strategies/${deleteTarget.id}`, { method: 'DELETE' });
      toast('success', 'Strategy deleted');
    } catch (err) {
      logger.error('Strategies', 'Failed to delete strategy', err);
      toast('error', `Failed to delete: ${describeApiError(err, 'unknown error')}`);
    }
    setDeleteTarget(null);
    void list.refetch();
  };

  const handleExport = async (id: string, name: string) => {
    try {
      // The export answers raw YAML, which fetchApi's JSON parse would reject.
      const yaml = await fetchApiText(`/api/strategies/${id}/export`);
      const blob = new Blob([yaml], { type: 'application/x-yaml' });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${id}.yaml`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);
      toast('success', `Exported "${name}"`);
    } catch (err) {
      logger.error('Strategies', 'Failed to export strategy', err);
      toast('error', describeApiError(err, 'Failed to export strategy'));
    }
  };

  const handleImport = async (file: File) => {
    try {
      const body = new FormData();
      body.append('file', file, file.name);
      await fetchApi('/api/strategies/import', { method: 'POST', body });
      toast('success', 'Strategy imported successfully');
      void list.refetch();
    } catch (err) {
      logger.error('Strategies', 'Failed to import strategy', err);
      toast('error', `Import failed: ${describeApiError(err, 'invalid file')}`);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <ResearchNav current="/strategies" />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink-1">Strategies</h1>
          <p className="mt-1 text-sm text-ink-2">
            {loading
              ? 'Loading…'
              : loadError !== null
                ? 'Strategies unavailable'
                : `${(strategies ?? []).filter((s) => s.enabled).length} active strategies`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" onClick={() => router.push('/templates')}>
            <LayoutTemplate className="h-4 w-4" />
            Browse Templates
          </Button>
          <Button
            variant="outline"
            onClick={() => fileInputRef.current?.click()}
          >
            <Upload className="h-4 w-4" />
            Import Strategy
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".yaml,.yml"
            aria-label="Import strategy YAML file"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) handleImport(file);
              e.target.value = '';
            }}
          />
          <Button variant="primary" onClick={() => router.push('/builder')}>
            <Plus className="h-4 w-4" />
            New Strategy
          </Button>
        </div>
      </div>

      {loadError !== null && (
        <DataStateErrorCard error={loadError} fallback="Failed to load strategies" onRetry={() => void list.refetch()} />
      )}
      {sessions.error !== null && (
        <div data-testid="sessions-error">
          <DataStateErrorCard error={sessions.error} fallback="Trading sessions unavailable; session controls are hidden" />
        </div>
      )}
      {config.error !== null && (
        <div data-testid="config-error">
          <DataStateErrorCard error={config.error} fallback="System config unavailable; trading mode is unknown" />
        </div>
      )}

      {!loading && loadError === null && (strategies ?? []).length === 0 && (
        <div className="flex flex-col items-center gap-3 rounded-lg border border-line bg-surface py-12 text-center">
          <p className="text-sm text-ink-2">No strategies yet. Build one or start from a template.</p>
          <div className="flex flex-wrap justify-center gap-2">
            <Button variant="primary" onClick={() => router.push('/builder')}>
              <Plus className="h-4 w-4" aria-hidden="true" />
              New strategy
            </Button>
            <Button variant="outline" onClick={() => router.push('/templates')}>
              <LayoutTemplate className="h-4 w-4" aria-hidden="true" />
              New from template
            </Button>
          </div>
        </div>
      )}

      {/* Strategy cards grid */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {(strategies ?? []).map((strategy) => {
          const session = sessionFor(strategy.id);
          const performance = perfMap[strategy.id] ?? null;
          const performanceError = perfErrors[strategy.id] ?? null;
          const driftDetail = driftMap[strategy.id] ?? null;
          const driftError = driftErrors[strategy.id] ?? null;

          return (
            <div
              key={strategy.id}
              className="rounded-lg border border-line bg-surface p-5 transition-colors hover:border-line-strong"
            >
              {/* Card header */}
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="text-sm font-semibold text-ink-1">
                    {strategy.name}
                  </h2>
                  <p className="mt-0.5 text-xs text-ink-2 line-clamp-1">
                    {strategy.description}
                  </p>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <StatusBadge
                    status={strategy.enabled ? 'Active' : 'Draft'}
                    variant={strategy.enabled ? 'success' : 'neutral'}
                    size="sm"
                  />
                  {session && (
                    <button
                      onClick={() => router.push(`/trading/${session.session_id}`)}
                      className="cursor-pointer"
                    >
                      <StatusBadge
                        status={sessionStatusLabel(session)}
                        variant={sessionStatusVariant(session)}
                        size="sm"
                      />
                    </button>
                  )}
                </div>
              </div>

              {/* Performance metrics */}
              {performanceError !== null && (
                <div className="mt-3 flex items-center gap-2 text-xs text-ink-2" data-testid="performance-error">
                  <DataStateBadge
                    state={stateFromError(performanceError)}
                    reason={describeApiError(performanceError, 'Performance unavailable')}
                  />
                  Performance: {describeApiError(performanceError, 'unavailable')}
                </div>
              )}
              {performance && (
                <div className="mt-3 grid grid-cols-5 gap-2 rounded-lg border border-line bg-surface p-2.5">
                  <div className="text-center">
                    <p className="text-sm font-bold text-ink-1">
                      <span aria-hidden="true">{signed(performance.total_pnl)}</span>
                      <span className="sr-only">{signedLabel(performance.total_pnl)}</span>
                    </p>
                    <p className="text-xs text-ink-2">PnL (USDT)</p>
                  </div>
                  <div className="text-center">
                    <p className="text-sm font-bold text-ink-1">
                      {pct(performance.win_rate)}
                    </p>
                    <p className="text-xs text-ink-2">Win rate</p>
                  </div>
                  <div className="text-center">
                    <p className="text-sm font-bold text-ink-1">
                      {performance.total_trades}
                    </p>
                    <p className="text-xs text-ink-2">Trades</p>
                  </div>
                  <div className="text-center">
                    <p className={cn(
                      'text-sm font-bold',
                      sharpeClass(performance.sharpe_ratio),
                    )}>
                      {performance.sharpe_ratio === null ? '—' : performance.sharpe_ratio.toFixed(2)}
                    </p>
                    <p className="text-xs text-ink-2">Sharpe</p>
                  </div>
                  <div className="text-center">
                    <p className={cn(
                      'text-sm font-bold',
                      performance.max_drawdown === null ? 'text-ink-2' : 'text-danger',
                    )}>
                      {performance.max_drawdown === null ? '—' : pct(performance.max_drawdown)}
                    </p>
                    <p className="text-xs text-ink-2">Max DD</p>
                  </div>
                </div>
              )}

              {/* Hot-reload state — only fetched for strategies with a running session */}
              {driftDetail !== null && (
                <StrategyDriftPanel
                  strategyId={strategy.id}
                  drift={driftDetail}
                  onRefetch={() => void refetchDrift(strategy.id)}
                />
              )}
              {driftError !== null && (
                <div className="mt-3 flex items-center gap-2 text-xs text-ink-2" data-testid="drift-error">
                  <DataStateBadge
                    state={stateFromError(driftError)}
                    reason={describeApiError(driftError, 'Hot-reload state unavailable')}
                  />
                  Hot-reload state: {describeApiError(driftError, 'unavailable')}
                </div>
              )}

              {/* Trading controls -- only show on Active strategies or when a session is running */}
              {sessions.error === null && (strategy.enabled || session) && (
                <div className="mt-3 flex items-center gap-2">
                  {session ? (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => router.push(`/trading/${session.session_id}`)}
                    >
                      <Eye className="h-3 w-3" aria-hidden="true" />
                      View session
                    </Button>
                  ) : (
                    (tradingMode === 'paper' || tradingMode === 'testnet') && (
                      <Button variant="outline" size="sm" onClick={() => setStartId(strategy.id)}>
                        <Play className="h-3 w-3" aria-hidden="true" />
                        Start paper session
                      </Button>
                    )
                  )}
                </div>
              )}

              {/* Action buttons */}
              <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
                <div className="flex flex-wrap items-center gap-2">
                  {strategy.editable && (
                    <button
                      onClick={() => router.push(`/builder?strategy=${strategy.id}`)}
                      className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-ink-2 hover:bg-hover hover:text-ink-1 transition-colors max-lg:h-11"
                    >
                      <Edit2 className="h-3 w-3" />
                      Edit
                    </button>
                  )}
                  <button
                    onClick={() => router.push(`/backtest?strategy=${strategy.id}`)}
                    className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-ink-2 hover:bg-hover hover:text-ink-1 transition-colors max-lg:h-11"
                  >
                    <BarChart3 className="h-3 w-3" />
                    Backtest
                  </button>
                  <button
                    onClick={() => handleExport(strategy.id, strategy.name)}
                    className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-ink-2 hover:bg-hover hover:text-ink-1 transition-colors max-lg:h-11"
                  >
                    <Download className="h-3 w-3" />
                    Export
                  </button>
                  <button
                    onClick={() => openDelete(strategy)}
                    className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-ink-2 hover:bg-danger-soft hover:text-danger transition-colors max-lg:h-11"
                  >
                    <Trash2 className="h-3 w-3" />
                    Delete
                  </button>
                </div>
                <div
                  className={cn(
                    'flex items-center max-lg:min-h-11',
                    session
                      ? 'cursor-not-allowed'
                      : 'cursor-pointer',
                  )}
                  role="switch"
                  aria-checked={strategy.enabled}
                  aria-disabled={!!session}
                  aria-label={`${strategy.enabled ? 'Disable' : 'Enable'} ${strategy.name}`}
                  tabIndex={session ? -1 : 0}
                  onClick={() => { if (!session) handleToggle(strategy.id); }}
                  onKeyDown={(e) => { if (!session && (e.key === 'Enter' || e.key === ' ')) handleToggle(strategy.id); }}
                >
                  <div
                    aria-hidden="true"
                    className={cn(
                      'relative h-6 w-11 rounded-full transition-colors',
                      session && 'opacity-50',
                      strategy.enabled
                        ? 'bg-ink-2'
                        : 'bg-line',
                    )}
                  >
                    <div
                      className={cn(
                        'absolute top-1 h-4 w-4 rounded-full shadow transition-transform',
                        strategy.enabled
                          ? 'translate-x-[22px] bg-canvas'
                          : 'translate-x-1 bg-ink-1',
                      )}
                    />
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Start door: mounted only while open (needs the shell status context). */}
      {startId !== null && <StartSessionDialog strategyId={startId} onClose={() => setStartId(null)} />}

      {/* Delete confirm */}
      {deleteTarget !== null && (
        <ConfirmDialog
          open
          destructive
          title={`Delete ${deleteTarget.name}?`}
          primaryLabel="Delete strategy"
          secondaryLabel="Keep it"
          pendingLabel="Deleting…"
          pendingHint="Closing this won't cancel the delete."
          onConfirm={confirmDelete}
          onClose={() => setDeleteTarget(null)}
        >
          <p>This removes the strategy&apos;s YAML file. This cannot be undone.</p>
          {sessions.data === null && sessions.error !== null ? (
            <p>Couldn&apos;t check for a running session.</p>
          ) : sessionFor(deleteTarget.id) !== null ? (
            <p>A running session keeps running on the config it started with; stop it from its session page.</p>
          ) : (
            <p>No session is running on it.</p>
          )}
        </ConfirmDialog>
      )}
    </div>
  );
}
