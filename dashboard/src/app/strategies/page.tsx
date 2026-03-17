'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Zap, Plus, Edit2, Trash2, BarChart3, Play, Square, Eye } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
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

function mapBuilderStrategies(data: BuilderStrategy[], sessions: TradingSession[]): Strategy[] {
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
    };
  });
}

function sessionStatusLabel(session: TradingSession | null): string {
  if (!session) return '';
  return session.trading_mode === 'paper' ? 'Paper Running' : 'Live Running';
}

function sessionStatusVariant(session: TradingSession | null): 'success' | 'warning' | 'info' | 'neutral' {
  if (!session) return 'neutral';
  return session.trading_mode === 'paper' ? 'info' : 'warning';
}

/* ---------- Page ---------- */

export default function StrategiesPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [sessions, setSessions] = useState<TradingSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [startingId, setStartingId] = useState<string | null>(null);
  const [stoppingId, setStoppingId] = useState<string | null>(null);
  const [tradingMode, setTradingMode] = useState<string>('paper');
  const [paperCapital, setPaperCapital] = useState<number>(10000);

  const fetchData = async () => {
    setLoading(true);
    try {
      const [stratData, sessData, cfg] = await Promise.all([
        fetchApi<BuilderStrategy[]>('/api/builder/strategies').catch(() => [] as BuilderStrategy[]),
        fetchApi<TradingSession[]>('/api/trading/sessions').catch(() => [] as TradingSession[]),
        fetchApi<{ trading_mode: string; paper_capital?: number }>('/api/system/config').catch(() => null),
      ]);
      setSessions(sessData);
      setStrategies(mapBuilderStrategies(stratData, sessData));
      if (cfg) {
        setTradingMode(cfg.trading_mode);
        if (cfg.paper_capital) setPaperCapital(cfg.paper_capital);
      }
    } catch (err) {
      logger.warn('Strategies', 'Failed to fetch strategies', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchData(); }, []);

  const handleToggle = async (id: string) => {
    try {
      await fetchApi(`/api/builder/strategies/${id}/toggle`, { method: 'POST' });
      toast('success', 'Strategy toggled');
      fetchData();
    } catch (err) {
      logger.error('Strategies', 'Failed to toggle strategy', err);
      toast('error', 'Failed to toggle strategy');
    }
  };

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete strategy "${name}"? This cannot be undone.`)) return;
    setDeletingId(id);
    try {
      await fetchApi(`/api/builder/strategies/${id}`, { method: 'DELETE' });
      toast('success', 'Strategy deleted');
      fetchData();
    } catch (err) {
      logger.error('Strategies', 'Failed to delete strategy', err);
      toast('error', `Failed to delete: ${err instanceof Error ? err.message : 'unknown error'}`);
    } finally {
      setDeletingId(null);
    }
  };

  const handleStartPaper = async (strategyId: string) => {
    setStartingId(strategyId);
    try {
      await fetchApi('/api/trading/sessions', {
        method: 'POST',
        body: JSON.stringify({
          strategy_id: strategyId,
          trading_mode: 'paper',
          paper_capital: paperCapital,
        }),
      });
      toast('success', 'Paper session started');
      fetchData();
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
      fetchData();
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
      fetchData();
    } catch (err) {
      logger.error('Strategies', 'Failed to stop session', err);
      toast('error', `Failed to stop: ${err instanceof Error ? err.message : 'unknown error'}`);
    } finally {
      setStoppingId(null);
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
        <Button variant="primary" onClick={() => router.push('/builder')}>
          <Plus className="h-4 w-4" />
          New Strategy
        </Button>
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

            {/* Trading controls — only show on Active strategies or when a session is running */}
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
