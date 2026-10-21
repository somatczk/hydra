'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { Zap, Plus, Edit2, Trash2, BarChart3, Play, Eye, Download, Upload, LayoutTemplate } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { Input } from '@/components/ui/Input';
import { cn } from '@/components/ui/cn';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { describeApiError, stateFromError } from '@/lib/apiState';
import { logger } from '@/lib/logger';
import { ResearchNav } from '@/components/research/ResearchNav';

/* ---------- Types ---------- */

interface TradingSession {
  session_id: string;
  strategy_id: string;
  trading_mode: string;
  status: string;
  started_at: string | null;
  stopped_at: string | null;
}

interface StrategyPerformance {
  total_pnl: number;
  win_rate: number;
  total_trades: number;
  sharpe_ratio: number | null;
  max_drawdown: number | null;
}

interface Strategy {
  id: string;
  name: string;
  description: string;
  status: 'Active' | 'Draft';
  statusVariant: 'success' | 'neutral';
  enabled: boolean;
  editable: boolean;
  session: TradingSession | null;
  performance: StrategyPerformance | null;
  performanceError: unknown;
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

function mapBuilderStrategies(
  data: BuilderStrategy[],
  sessions: TradingSession[],
  perfMap: Record<string, StrategyPerformance>,
  perfErrors: Record<string, unknown>,
): Strategy[] {
  return data.map((s) => {
    const session = sessions.find(
      (sess) => sess.strategy_id === s.id && sess.status === 'running'
    ) ?? null;

    return {
      id: s.id,
      name: s.name,
      description: s.description || 'Rule-based strategy',
      status: s.enabled ? 'Active' : 'Draft',
      statusVariant: s.enabled ? 'success' : 'neutral',
      enabled: s.enabled,
      editable: s.editable,
      session,
      performance: perfMap[s.id] ?? null,
      performanceError: perfErrors[s.id] ?? null,
    };
  });
}

function sessionStatusLabel(session: TradingSession | null): string {
  if (!session) return '';
  const mode = session.trading_mode === 'paper' ? 'Paper Running' : 'Live Running';
  if (session.started_at) {
    const dt = new Date(session.started_at);
    return `${mode} · ${dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}, ${dt.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}`;
  }
  return mode;
}

function sessionStatusVariant(session: TradingSession | null): 'success' | 'warning' | 'info' | 'neutral' {
  if (!session) return 'neutral';
  return session.trading_mode === 'paper' ? 'info' : 'warning';
}

function fmtPnl(v: number): string {
  const sign = v >= 0 ? '+' : '-';
  return `${sign}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
}

function sharpeClass(v: number | null): string {
  if (v === null) return 'text-ink-2';
  if (v >= 1) return 'text-ink-1';
  return v >= 0 ? 'text-ink-1' : 'text-danger';
}

/* ---------- Page ---------- */

export default function StrategiesPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(true);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [startingId, setStartingId] = useState<string | null>(null);
  const [tradingMode, setTradingMode] = useState<string | null>(null);
  const [paperCapital, setPaperCapital] = useState<number | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [sessionsError, setSessionsError] = useState<unknown>(null);
  const [configError, setConfigError] = useState<unknown>(null);
  const [capitalMap, setCapitalMap] = useState<Record<string, string>>({});
  const fileInputRef = useRef<HTMLInputElement>(null);

  const fetchData = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const [stratResult, sessResult, cfgResult] = await Promise.allSettled([
        fetchApi<BuilderStrategy[]>('/api/strategies'),
        fetchApi<TradingSession[]>('/api/trading/sessions'),
        fetchApi<{ trading_mode: string; paper_capital?: number }>('/api/system/config'),
      ]);
      if (stratResult.status === 'rejected') {
        logger.warn('Strategies', 'Failed to fetch strategies', stratResult.reason);
        setLoadError(stratResult.reason);
        setStrategies([]);
        return;
      }
      setLoadError(null);
      const stratData = stratResult.value;
      const sessData = sessResult.status === 'fulfilled' ? sessResult.value : [];
      setSessionsError(sessResult.status === 'rejected' ? sessResult.reason : null);
      const cfg = cfgResult.status === 'fulfilled' ? cfgResult.value : null;
      setConfigError(cfgResult.status === 'rejected' ? cfgResult.reason : null);

      // Fetch performance for each strategy in parallel
      const perfEntries = await Promise.allSettled(
        stratData.map((s) => fetchApi<StrategyPerformance>(`/api/strategies/${s.id}/performance`)),
      );
      const perfMap: Record<string, StrategyPerformance> = {};
      const perfErrors: Record<string, unknown> = {};
      perfEntries.forEach((entry, i) => {
        const id = stratData[i].id;
        if (entry.status === 'fulfilled') perfMap[id] = entry.value;
        else perfErrors[id] = entry.reason;
      });

      setStrategies(mapBuilderStrategies(stratData, sessData, perfMap, perfErrors));
      setTradingMode(cfg ? cfg.trading_mode : null);
      setPaperCapital(cfg?.paper_capital ?? null);
      const configuredCapital = cfg?.paper_capital;
      // Initialize capital for new strategies (don't overwrite user edits)
      if (configuredCapital) {
        setCapitalMap((prev) => {
          const next = { ...prev };
          for (const s of stratData) {
            if (!(s.id in next)) {
              next[s.id] = String(configuredCapital);
            }
          }
          return next;
        });
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(() => fetchData(true), 30_000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const handleToggle = async (id: string) => {
    try {
      await fetchApi(`/api/strategies/${id}/toggle`, { method: 'POST' });
      toast('success', 'Strategy toggled');
      fetchData(true);
    } catch (err) {
      logger.error('Strategies', 'Failed to toggle strategy', err);
      toast('error', describeApiError(err, 'Failed to toggle strategy'));
    }
  };

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete strategy "${name}"? This cannot be undone.`)) return;
    setDeletingId(id);
    try {
      await fetchApi(`/api/strategies/${id}`, { method: 'DELETE' });
      toast('success', 'Strategy deleted');
      fetchData(true);
    } catch (err) {
      logger.error('Strategies', 'Failed to delete strategy', err);
      toast('error', `Failed to delete: ${describeApiError(err, 'unknown error')}`);
    } finally {
      setDeletingId(null);
    }
  };

  const handleStartPaper = async (strategyId: string) => {
    const capital = parseFloat(capitalMap[strategyId] ?? '') || paperCapital;
    if (!capital || capital <= 0) {
      toast('error', 'Enter the paper capital to start a session');
      return;
    }
    setStartingId(strategyId);
    try {
      await fetchApi('/api/trading/sessions', {
        method: 'POST',
        body: JSON.stringify({
          strategy_id: strategyId,
          trading_mode: 'paper',
          paper_capital: capital,
        }),
      });
      toast('success', 'Paper session started');
      fetchData(true);
    } catch (err) {
      logger.error('Strategies', 'Failed to start paper session', err);
      toast('error', `Failed to start: ${describeApiError(err, 'unknown error')}`);
    } finally {
      setStartingId(null);
    }
  };

  const handleExport = async (id: string, name: string) => {
    try {
      const yaml = await fetchApi<string>(`/api/strategies/${id}/export`);
      const blob = new Blob([typeof yaml === 'string' ? yaml : JSON.stringify(yaml, null, 2)], { type: 'application/x-yaml' });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${name.toLowerCase().replace(/\s+/g, '_')}.yaml`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);
      toast('success', `Exported "${name}"`);
    } catch (err) {
      logger.error('Strategies', 'Failed to export strategy', err);
      toast('error', 'Failed to export strategy');
    }
  };

  const handleImport = async (file: File) => {
    try {
      const content = await file.text();
      await fetchApi('/api/strategies/import', {
        method: 'POST',
        body: JSON.stringify({ yaml_content: content }),
      });
      toast('success', 'Strategy imported successfully');
      fetchData(true);
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
              ? 'Loading...'
              : loadError !== null
                ? 'Strategies unavailable'
                : `${strategies.filter((s) => s.status === 'Active').length} active strategies`}
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
        <DataStateErrorCard error={loadError} fallback="Failed to load strategies" onRetry={() => fetchData()} />
      )}
      {sessionsError !== null && (
        <div data-testid="sessions-error">
          <DataStateErrorCard error={sessionsError} fallback="Trading sessions unavailable; session controls are hidden" />
        </div>
      )}
      {configError !== null && (
        <div data-testid="config-error">
          <DataStateErrorCard error={configError} fallback="System config unavailable; trading mode and capital are unknown" />
        </div>
      )}

      {!loading && loadError === null && strategies.length === 0 && (
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
        {strategies.map((strategy) => (
          <div
            key={strategy.id}
            className="rounded-xl border border-line bg-surface p-5 transition-colors hover:border-line-strong"
          >
            {/* Card header */}
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-raised">
                  <Zap className="h-5 w-5 text-accent" aria-hidden="true" />
                </div>
                <div>
                  <h2 className="text-sm font-semibold text-ink-1">
                    {strategy.name}
                  </h2>
                  <p className="mt-0.5 text-xs text-ink-2 line-clamp-1">
                    {strategy.description}
                  </p>
                </div>
              </div>
              <div className="flex flex-col items-end gap-1">
                <StatusBadge
                  status={strategy.status}
                  variant={strategy.statusVariant}
                  size="sm"
                />
                {strategy.session && (
                  <button
                    onClick={() => router.push(`/trading/${strategy.session!.session_id}`)}
                    className="cursor-pointer"
                  >
                    <StatusBadge
                      status={sessionStatusLabel(strategy.session)}
                      variant={sessionStatusVariant(strategy.session)}
                      size="sm"
                    />
                  </button>
                )}
              </div>
            </div>

            {/* Performance metrics */}
            {strategy.performanceError !== null && (
              <div className="mt-3 flex items-center gap-2 text-xs text-ink-2" data-testid="performance-error">
                <DataStateBadge
                  state={stateFromError(strategy.performanceError)}
                  reason={describeApiError(strategy.performanceError, 'Performance unavailable')}
                />
                Performance: {describeApiError(strategy.performanceError, 'unavailable')}
              </div>
            )}
            {strategy.performance && (
              <div className="mt-3 grid grid-cols-5 gap-2 rounded-lg border border-line bg-surface p-2.5">
                <div className="text-center">
                  <p className={cn(
                    'text-sm font-bold',
                    strategy.performance.total_pnl >= 0 ? 'text-ink-1' : 'text-danger',
                  )}>
                    {fmtPnl(strategy.performance.total_pnl)}
                  </p>
                  <p className="text-[10px] text-ink-2">PnL</p>
                </div>
                <div className="text-center">
                  <p className="text-sm font-bold text-ink-1">
                    {strategy.performance.win_rate.toFixed(1)}%
                  </p>
                  <p className="text-[10px] text-ink-2">Win Rate</p>
                </div>
                <div className="text-center">
                  <p className="text-sm font-bold text-ink-1">
                    {strategy.performance.total_trades}
                  </p>
                  <p className="text-[10px] text-ink-2">Trades</p>
                </div>
                <div className="text-center">
                  <p className={cn(
                    'text-sm font-bold',
                    sharpeClass(strategy.performance.sharpe_ratio),
                  )}>
                    {strategy.performance.sharpe_ratio === null ? '—' : strategy.performance.sharpe_ratio.toFixed(2)}
                  </p>
                  <p className="text-[10px] text-ink-2">Sharpe</p>
                </div>
                <div className="text-center">
                  <p className={cn(
                    'text-sm font-bold',
                    strategy.performance.max_drawdown === null ? 'text-ink-2' : 'text-danger',
                  )}>
                    {strategy.performance.max_drawdown === null ? '—' : `${strategy.performance.max_drawdown.toFixed(1)}%`}
                  </p>
                  <p className="text-[10px] text-ink-2">Max DD</p>
                </div>
              </div>
            )}

            {/* Per-strategy capital — only when no session is running */}
            {!strategy.session && strategy.status === 'Active' && tradingMode === 'paper' && (
              <div className="mt-3 flex items-center gap-2">
                <label className="text-xs text-ink-2 whitespace-nowrap">Capital:</label>
                <Input
                  type="number"
                  value={capitalMap[strategy.id] ?? (paperCapital !== null ? String(paperCapital) : '')}
                  onChange={(e) =>
                    setCapitalMap((prev) => ({ ...prev, [strategy.id]: e.target.value }))
                  }
                  className="w-32"
                />
              </div>
            )}

            {/* Trading controls -- only show on Active strategies or when a session is running */}
            {sessionsError === null && (strategy.status === 'Active' || strategy.session) && (
              <div className="mt-3 flex items-center gap-2">
                {strategy.session ? (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => router.push(`/trading/${strategy.session!.session_id}`)}
                  >
                    <Eye className="h-3 w-3" aria-hidden="true" />
                    View session
                  </Button>
                ) : (
                  (tradingMode === 'paper' || tradingMode === 'testnet') && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => handleStartPaper(strategy.id)}
                      loading={startingId === strategy.id}
                    >
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
                    className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-ink-2 hover:bg-hover hover:text-ink-1 transition-colors"
                  >
                    <Edit2 className="h-3 w-3" />
                    Edit
                  </button>
                )}
                <button
                  onClick={() => router.push(`/backtest?strategy=${strategy.id}`)}
                  className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-ink-2 hover:bg-hover hover:text-ink-1 transition-colors"
                >
                  <BarChart3 className="h-3 w-3" />
                  Backtest
                </button>
                <button
                  onClick={() => handleExport(strategy.id, strategy.name)}
                  className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-ink-2 hover:bg-hover hover:text-ink-1 transition-colors"
                >
                  <Download className="h-3 w-3" />
                  Export
                </button>
                <button
                  onClick={() => handleDelete(strategy.id, strategy.name)}
                  disabled={deletingId === strategy.id}
                  className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-ink-2 hover:bg-danger-soft hover:text-danger transition-colors disabled:opacity-50"
                >
                  <Trash2 className="h-3 w-3" />
                  {deletingId === strategy.id ? 'Deleting...' : 'Delete'}
                </button>
              </div>
              <div
                className={cn(
                  'relative h-6 w-11 rounded-full transition-colors',
                  strategy.session
                    ? 'cursor-not-allowed opacity-50'
                    : 'cursor-pointer',
                  strategy.status === 'Active'
                    ? 'bg-ink-2'
                    : 'bg-line',
                )}
                role="switch"
                aria-checked={strategy.status === 'Active'}
                aria-disabled={!!strategy.session}
                aria-label={`${strategy.status === 'Active' ? 'Disable' : 'Enable'} ${strategy.name}`}
                tabIndex={strategy.session ? -1 : 0}
                onClick={() => { if (!strategy.session) handleToggle(strategy.id); }}
                onKeyDown={(e) => { if (!strategy.session && (e.key === 'Enter' || e.key === ' ')) handleToggle(strategy.id); }}
              >
                <div
                  className={cn(
                    'absolute top-1 h-4 w-4 rounded-full shadow transition-transform',
                    strategy.status === 'Active'
                      ? 'translate-x-[22px] bg-canvas'
                      : 'translate-x-1 bg-ink-1',
                  )}
                />
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
