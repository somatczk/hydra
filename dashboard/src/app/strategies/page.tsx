'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { Zap, Plus, Edit2, Trash2, BarChart3, Play, Square, Eye, Shield, ChevronDown, ChevronUp, Download, Upload, LayoutTemplate } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { Input } from '@/components/ui/Input';
import { cn } from '@/components/ui/cn';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { logger } from '@/lib/logger';

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
  sharpe_ratio: number;
  max_drawdown: number;
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
}

interface BuilderStrategy {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  filename: string;
  editable: boolean;
}

interface RiskOverrides {
  max_position_pct: number;
  max_risk_per_trade: number;
  max_portfolio_heat: number;
  max_daily_loss_pct: number;
  max_drawdown_pct: number;
  max_concurrent_positions: number;
}

/* ---------- Helpers ---------- */

function mapBuilderStrategies(
  data: BuilderStrategy[],
  sessions: TradingSession[],
  perfMap: Record<string, StrategyPerformance>,
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

/* ---------- Page ---------- */

export default function StrategiesPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(true);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [startingId, setStartingId] = useState<string | null>(null);
  const [stoppingId, setStoppingId] = useState<string | null>(null);
  const [tradingMode, setTradingMode] = useState<string>('paper');
  const [paperCapital, setPaperCapital] = useState<number>(10000);
  const [capitalMap, setCapitalMap] = useState<Record<string, string>>({});
  const [riskOverridesOpen, setRiskOverridesOpen] = useState<string | null>(null);
  const [riskOverrides, setRiskOverrides] = useState<Record<string, RiskOverrides>>({});
  const [savingRisk, setSavingRisk] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const fetchData = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const [stratData, sessData, cfg] = await Promise.all([
        fetchApi<BuilderStrategy[]>('/api/strategies').catch(() => [] as BuilderStrategy[]),
        fetchApi<TradingSession[]>('/api/trading/sessions').catch(() => [] as TradingSession[]),
        fetchApi<{ trading_mode: string; paper_capital?: number }>('/api/system/config').catch(() => null),
      ]);

      // Fetch performance for each strategy in parallel
      const perfEntries = await Promise.all(
        stratData.map((s) =>
          fetchApi<StrategyPerformance>(`/api/strategies/${s.id}/performance`)
            .then((perf) => [s.id, perf] as const)
            .catch(() => [s.id, null] as const)
        ),
      );
      const perfMap: Record<string, StrategyPerformance> = {};
      for (const [id, perf] of perfEntries) {
        if (perf) perfMap[id] = perf;
      }

      setStrategies(mapBuilderStrategies(stratData, sessData, perfMap));
      const effectiveCapital = cfg?.paper_capital ?? 10000;
      if (cfg) {
        setTradingMode(cfg.trading_mode);
        if (cfg.paper_capital) setPaperCapital(cfg.paper_capital);
      }
      // Initialize capital for new strategies (don't overwrite user edits)
      setCapitalMap((prev) => {
        const next = { ...prev };
        for (const s of stratData) {
          if (!(s.id in next)) {
            next[s.id] = String(effectiveCapital);
          }
        }
        return next;
      });
    } catch (err) {
      logger.warn('Strategies', 'Failed to fetch strategies', err);
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
      toast('error', 'Failed to toggle strategy');
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
      toast('error', `Failed to delete: ${err instanceof Error ? err.message : 'unknown error'}`);
    } finally {
      setDeletingId(null);
    }
  };

  const handleStartPaper = async (strategyId: string) => {
    setStartingId(strategyId);
    const capital = parseFloat(capitalMap[strategyId]) || paperCapital;
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
      toast('error', `Failed to start: ${err instanceof Error ? err.message : 'unknown error'}`);
    } finally {
      setStartingId(null);
    }
  };

  const handleStartLive = async (strategyId: string) => {
    if (!confirm('Start LIVE trading? Real funds will be used.')) return;
    setStartingId(strategyId);
    try {
      await fetchApi('/api/trading/sessions', {
        method: 'POST',
        body: JSON.stringify({
          strategy_id: strategyId,
          trading_mode: 'live',
        }),
      });
      toast('success', 'Live session started');
      fetchData(true);
    } catch (err) {
      logger.error('Strategies', 'Failed to start live session', err);
      toast('error', `Failed to start: ${err instanceof Error ? err.message : 'unknown error'}`);
    } finally {
      setStartingId(null);
    }
  };

  const handleStop = async (sessionId: string) => {
    setStoppingId(sessionId);
    try {
      await fetchApi(`/api/trading/sessions/${sessionId}`, { method: 'DELETE' });
      toast('success', 'Session stopped');
      fetchData(true);
    } catch (err) {
      logger.error('Strategies', 'Failed to stop session', err);
      toast('error', `Failed to stop: ${err instanceof Error ? err.message : 'unknown error'}`);
    } finally {
      setStoppingId(null);
    }
  };

  const toggleRiskOverrides = async (strategyId: string) => {
    if (riskOverridesOpen === strategyId) {
      setRiskOverridesOpen(null);
      return;
    }
    setRiskOverridesOpen(strategyId);
    // Load existing overrides if not cached
    if (!riskOverrides[strategyId]) {
      try {
        const cfg = await fetchApi<RiskOverrides>(`/api/risk/config/${strategyId}`);
        setRiskOverrides((prev) => ({ ...prev, [strategyId]: cfg }));
      } catch {
        // No overrides yet — use defaults
        setRiskOverrides((prev) => ({
          ...prev,
          [strategyId]: {
            max_position_pct: 0.10,
            max_risk_per_trade: 0.02,
            max_portfolio_heat: 0.06,
            max_daily_loss_pct: 0.03,
            max_drawdown_pct: 0.15,
            max_concurrent_positions: 10,
          },
        }));
      }
    }
  };

  const updateRiskField = (strategyId: string, field: keyof RiskOverrides, value: string) => {
    setRiskOverrides((prev) => ({
      ...prev,
      [strategyId]: {
        ...prev[strategyId],
        [field]: field === 'max_concurrent_positions' ? parseInt(value, 10) || 0 : parseFloat(value) / 100 || 0,
      },
    }));
  };

  const handleSaveRiskOverrides = async (strategyId: string) => {
    setSavingRisk(strategyId);
    try {
      const overrides = riskOverrides[strategyId];
      await fetchApi(`/api/risk/config/${strategyId}`, {
        method: 'PUT',
        body: JSON.stringify(overrides),
      });
      toast('success', 'Risk overrides saved');
    } catch (err) {
      logger.error('Strategies', 'Failed to save risk overrides', err);
      toast('error', 'Failed to save risk overrides');
    } finally {
      setSavingRisk(null);
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
      toast('error', `Import failed: ${err instanceof Error ? err.message : 'invalid file'}`);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-text-muted">
            {loading ? 'Loading...' : `${strategies.filter((s) => s.status === 'Active').length} active strategies`}
          </p>
        </div>
        <div className="flex items-center gap-2">
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

      {/* Strategy cards grid */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {strategies.map((strategy) => (
          <div
            key={strategy.id}
            className="rounded-xl border border-border-default bg-bg-elevated p-5 transition-colors hover:border-border-hover"
          >
            {/* Card header */}
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-bg-tertiary">
                  <Zap className="h-5 w-5 text-accent-primary" aria-hidden="true" />
                </div>
                <div>
                  <h3 className="text-sm font-semibold text-text-primary">
                    {strategy.name}
                  </h3>
                  <p className="mt-0.5 text-xs text-text-muted line-clamp-1">
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
            {strategy.performance && (
              <div className="mt-3 grid grid-cols-5 gap-2 rounded-lg border border-border-default bg-bg-secondary p-2.5">
                <div className="text-center">
                  <p className={cn(
                    'text-sm font-bold',
                    strategy.performance.total_pnl >= 0 ? 'text-status-success' : 'text-status-error',
                  )}>
                    {fmtPnl(strategy.performance.total_pnl)}
                  </p>
                  <p className="text-[10px] text-text-muted">PnL</p>
                </div>
                <div className="text-center">
                  <p className="text-sm font-bold text-text-primary">
                    {strategy.performance.win_rate.toFixed(1)}%
                  </p>
                  <p className="text-[10px] text-text-muted">Win Rate</p>
                </div>
                <div className="text-center">
                  <p className="text-sm font-bold text-text-primary">
                    {strategy.performance.total_trades}
                  </p>
                  <p className="text-[10px] text-text-muted">Trades</p>
                </div>
                <div className="text-center">
                  <p className={cn(
                    'text-sm font-bold',
                    strategy.performance.sharpe_ratio >= 1 ? 'text-status-success' : strategy.performance.sharpe_ratio >= 0 ? 'text-text-primary' : 'text-status-error',
                  )}>
                    {strategy.performance.sharpe_ratio.toFixed(2)}
                  </p>
                  <p className="text-[10px] text-text-muted">Sharpe</p>
                </div>
                <div className="text-center">
                  <p className="text-sm font-bold text-status-error">
                    {strategy.performance.max_drawdown.toFixed(1)}%
                  </p>
                  <p className="text-[10px] text-text-muted">Max DD</p>
                </div>
              </div>
            )}

            {/* Per-strategy capital — only when no session is running */}
            {!strategy.session && strategy.status === 'Active' && tradingMode === 'paper' && (
              <div className="mt-3 flex items-center gap-2">
                <label className="text-xs text-text-muted whitespace-nowrap">Capital:</label>
                <Input
                  type="number"
                  value={capitalMap[strategy.id] ?? String(paperCapital)}
                  onChange={(e) =>
                    setCapitalMap((prev) => ({ ...prev, [strategy.id]: e.target.value }))
                  }
                  className="w-32"
                />
              </div>
            )}

            {/* Trading controls -- only show on Active strategies or when a session is running */}
            {(strategy.status === 'Active' || strategy.session) && (
              <div className="mt-3 flex items-center gap-2">
                {strategy.session ? (
                  <>
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => handleStop(strategy.session!.session_id)}
                      loading={stoppingId === strategy.session.session_id}
                    >
                      <Square className="h-3 w-3" />
                      Stop
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => router.push(`/trading/${strategy.session!.session_id}`)}
                    >
                      <Eye className="h-3 w-3" />
                      View
                    </Button>
                  </>
                ) : (
                  <>
                    {tradingMode === 'paper' && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleStartPaper(strategy.id)}
                        loading={startingId === strategy.id}
                      >
                        <Play className="h-3 w-3" />
                        Start Paper
                      </Button>
                    )}
                    {tradingMode === 'live' && (
                      <Button
                        variant="primary"
                        size="sm"
                        onClick={() => handleStartLive(strategy.id)}
                        loading={startingId === strategy.id}
                      >
                        <Play className="h-3 w-3" />
                        Start Live
                      </Button>
                    )}
                    {tradingMode === 'testnet' && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleStartPaper(strategy.id)}
                        loading={startingId === strategy.id}
                      >
                        <Play className="h-3 w-3" />
                        Start Testnet
                      </Button>
                    )}
                  </>
                )}
              </div>
            )}

            {/* Risk overrides */}
            <div className="mt-3 border-t border-border-default pt-3">
              <button
                onClick={() => toggleRiskOverrides(strategy.id)}
                className="inline-flex items-center gap-1 text-xs text-text-muted hover:text-text-primary transition-colors"
              >
                <Shield className="h-3 w-3" />
                Risk Overrides
                {riskOverridesOpen === strategy.id ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
              </button>
              {riskOverridesOpen === strategy.id && riskOverrides[strategy.id] && (
                <div className="mt-3 space-y-3 rounded-lg border border-border-default bg-bg-secondary p-3">
                  <div className="grid grid-cols-2 gap-3">
                    <Input
                      label="Max Position %"
                      type="number"
                      value={(riskOverrides[strategy.id].max_position_pct * 100).toFixed(0)}
                      onChange={(e) => updateRiskField(strategy.id, 'max_position_pct', e.target.value)}
                    />
                    <Input
                      label="Max Risk/Trade %"
                      type="number"
                      value={(riskOverrides[strategy.id].max_risk_per_trade * 100).toFixed(0)}
                      onChange={(e) => updateRiskField(strategy.id, 'max_risk_per_trade', e.target.value)}
                    />
                    <Input
                      label="Portfolio Heat %"
                      type="number"
                      value={(riskOverrides[strategy.id].max_portfolio_heat * 100).toFixed(0)}
                      onChange={(e) => updateRiskField(strategy.id, 'max_portfolio_heat', e.target.value)}
                    />
                    <Input
                      label="Max Daily Loss %"
                      type="number"
                      value={(riskOverrides[strategy.id].max_daily_loss_pct * 100).toFixed(0)}
                      onChange={(e) => updateRiskField(strategy.id, 'max_daily_loss_pct', e.target.value)}
                    />
                    <Input
                      label="Max Drawdown %"
                      type="number"
                      value={(riskOverrides[strategy.id].max_drawdown_pct * 100).toFixed(0)}
                      onChange={(e) => updateRiskField(strategy.id, 'max_drawdown_pct', e.target.value)}
                    />
                    <Input
                      label="Max Concurrent Positions"
                      type="number"
                      value={riskOverrides[strategy.id].max_concurrent_positions.toString()}
                      onChange={(e) => updateRiskField(strategy.id, 'max_concurrent_positions', e.target.value)}
                    />
                  </div>
                  <div className="flex justify-end">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => handleSaveRiskOverrides(strategy.id)}
                      loading={savingRisk === strategy.id}
                    >
                      Save Overrides
                    </Button>
                  </div>
                </div>
              )}
            </div>

            {/* Action buttons */}
            <div className="mt-3 flex items-center justify-between border-t border-border-default pt-3">
              <div className="flex items-center gap-2">
                {strategy.editable && (
                  <button
                    onClick={() => router.push(`/builder?strategy=${strategy.id}`)}
                    className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-text-muted hover:bg-bg-hover hover:text-text-primary transition-colors"
                  >
                    <Edit2 className="h-3 w-3" />
                    Edit
                  </button>
                )}
                <button
                  onClick={() => router.push(`/backtest?strategy=${strategy.id}`)}
                  className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-text-muted hover:bg-bg-hover hover:text-text-primary transition-colors"
                >
                  <BarChart3 className="h-3 w-3" />
                  Backtest
                </button>
                <button
                  onClick={() => handleExport(strategy.id, strategy.name)}
                  className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-text-muted hover:bg-bg-hover hover:text-text-primary transition-colors"
                >
                  <Download className="h-3 w-3" />
                  Export
                </button>
                <button
                  onClick={() => handleDelete(strategy.id, strategy.name)}
                  disabled={deletingId === strategy.id}
                  className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-text-muted hover:bg-status-error/10 hover:text-status-error transition-colors disabled:opacity-50"
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
                    ? 'bg-status-success'
                    : 'bg-bg-active',
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
                    'absolute top-1 h-4 w-4 rounded-full bg-white shadow transition-transform',
                    strategy.status === 'Active'
                      ? 'translate-x-[22px]'
                      : 'translate-x-1',
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
