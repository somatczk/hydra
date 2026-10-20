'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { ShieldCheck, AlertTriangle, Ban, OctagonX, Activity } from 'lucide-react';
import {
  RadialBarChart,
  RadialBar,
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Cell,
} from 'recharts';
import { DataCard } from '@/components/ui/DataCard';
import { Input } from '@/components/ui/Input';
import { Button } from '@/components/ui/Button';
import { cn } from '@/components/ui/cn';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { logger } from '@/lib/logger';
import { describeApiError, parseErrorDetail, stateFromError, type DataState } from '@/lib/apiState';
import {
  buildRiskLimitsUpdate,
  riskLimitsToForm,
  type RiskLimitField,
  type RiskLimitsForm,
} from '@/lib/riskLimits';

/* ---------- Types ---------- */

interface CircuitBreaker {
  tier: string;
  label: string;
  threshold: string;
  currentValue: string;
  status: 'Normal' | 'Warning' | 'Tripped';
  activeSince: string | null;
  requiresManualReview: boolean;
  color: string;
  bgColor: string;
  borderColor: string;
  icon: typeof ShieldCheck;
}

interface ApiCircuitBreaker {
  tier: number;
  label: string;
  threshold: string;
  threshold_pct: number;
  current_value: number;
  status: 'Normal' | 'Warning' | 'Tripped';
  active_since: string | null;
  requires_manual_review: boolean;
}

interface Restrictions {
  active_tier: number;
  can_open_new_trades: boolean;
  size_multiplier: number;
  must_flatten: boolean;
  is_shutdown: boolean;
  daily_halt_until: string | null;
  consecutive_pause_until: string | null;
}

interface ApiRiskStatus {
  current_drawdown: number;
  max_drawdown_limit: number;
  daily_loss: number | null;
  daily_loss_limit: number | null;
  equity: number | null;
  peak_equity: number | null;
  day_start_equity: number | null;
  consecutive_losses: number;
  max_consecutive_losses: number;
  circuit_breakers: ApiCircuitBreaker[] | null;
  circuit_breakers_state: 'live' | 'unavailable';
  restrictions: Restrictions | null;
}

interface RiskEvent {
  id: number | null;
  ts: string | null;
  kind: string;
  tier: number | null;
  actor: string;
  reason: string;
}

interface RiskConfig {
  scope: string;
  max_position_pct: number;
  max_risk_per_trade: number;
  max_portfolio_heat: number;
  max_daily_loss_pct: number;
  max_drawdown_pct: number;
  max_concurrent_positions: number;
  kill_switch_active: boolean;
}

interface VarEstimate {
  var_95: number;
  var_99: number;
  cvar_95: number;
  portfolio_value: number;
  calculation_method: string;
  sample_days: number;
}

interface LiveStatus {
  kill_switch_active: boolean | null;
  kill_switch_state: 'active' | 'inactive' | 'unknown';
  running_sessions: number | null;
  circuit_breaker_restrictions: Restrictions | null;
  circuit_breakers_state: 'live' | 'unavailable';
}

interface KillSwitchResponse {
  active: boolean;
  message: string;
  sessions_stopped: number;
  positions_remaining: number;
  persisted: boolean;
  flatten_failures: string[];
}

/** What the last activation left behind: positions it could not flatten, an unsaved latch. */
interface KillSwitchRemainder {
  positionsRemaining: number;
  flattenFailures: string[];
  persisted: boolean;
}

function remainderFrom(body: Record<string, unknown>, persisted: boolean): KillSwitchRemainder | null {
  const n = Number(body.positions_remaining);
  const positionsRemaining = Number.isFinite(n) && n > 0 ? n : 0;
  const failures = Array.isArray(body.flatten_failures) ? body.flatten_failures.map(String) : [];
  if (positionsRemaining === 0 && persisted) return null;
  return { positionsRemaining, flattenFailures: failures, persisted };
}

interface SessionRemainder {
  positions_at_stop: number;
  flatten_incomplete: boolean;
}

/** Positions the sessions still hold after a flatten that did not finish (server state). */
function remainingFromSessions(sessions: SessionRemainder[]): number {
  return sessions.reduce(
    (sum, s) => sum + (s.flatten_incomplete ? Math.max(0, Number(s.positions_at_stop) || 0) : 0),
    0,
  );
}

function latchedText(n: number): string {
  return `LATCHED — ${n} ${n === 1 ? 'position' : 'positions'} could not be flattened`;
}

const MIN_VAR_SAMPLE_DAYS = 5;
const EVENTS_PATH = '/api/risk/events?limit=20';

/* ---------- Helpers ---------- */

function statusToColors(s: string) {
  switch (s) {
    case 'Warning':
      return { color: 'text-status-warning', bgColor: 'bg-status-warning/10', borderColor: 'border-status-warning/30', icon: AlertTriangle };
    case 'Tripped':
      return { color: 'text-status-error', bgColor: 'bg-status-error/10', borderColor: 'border-status-error/30', icon: Ban };
    default:
      return { color: 'text-status-success', bgColor: 'bg-status-success/10', borderColor: 'border-status-success/30', icon: ShieldCheck };
  }
}

interface RiskState {
  /** null while the engine's risk service is not available */
  circuitBreakers: CircuitBreaker[] | null;
  drawdown: number;
  maxDrawdown: number;
  dailyLoss: number | null;
  consecutiveLosses: number;
  maxConsecutiveLosses: number;
  restrictions: Restrictions | null;
}

function mapApiRiskStatus(data: ApiRiskStatus): RiskState {
  const breakers: CircuitBreaker[] | null = data.circuit_breakers?.map((cb) => {
    const colors = statusToColors(cb.status);
    // Tier 4 always uses error colour scheme even when Normal
    const finalColors = cb.tier === 4
      ? { color: 'text-status-error', bgColor: 'bg-status-error/10', borderColor: 'border-status-error/30', icon: Ban }
      : colors;
    return {
      tier: `Tier ${cb.tier}`,
      label: cb.label,
      threshold: cb.threshold,
      currentValue: `${cb.current_value}%`,
      status: cb.status,
      activeSince: cb.active_since,
      requiresManualReview: cb.requires_manual_review,
      ...finalColors,
    };
  }) ?? null;
  return {
    circuitBreakers: breakers,
    drawdown: data.current_drawdown,
    maxDrawdown: data.max_drawdown_limit,
    dailyLoss: data.daily_loss,
    consecutiveLosses: data.consecutive_losses,
    maxConsecutiveLosses: data.max_consecutive_losses,
    restrictions: data.restrictions,
  };
}

function fmtTime(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString('en-GB', { timeZone: 'UTC' }) + ' UTC' : '';
}

interface DailyPnl { date: string; pnl: number; }

function fmtUsd(v: number): string {
  return `$${v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/* ---------- Skeleton ---------- */

function CardSkeleton() {
  return (
    <div className="animate-pulse rounded-xl border border-border-default bg-bg-elevated p-5">
      <div className="h-4 w-24 rounded bg-bg-tertiary" />
      <div className="mt-3 h-6 w-16 rounded bg-bg-tertiary" />
      <div className="mt-2 h-3 w-32 rounded bg-bg-tertiary" />
    </div>
  );
}

/* ---------- Page ---------- */

export default function RiskPage() {
  const { toast } = useToast();
  const [state, setState] = useState<RiskState | null>(null);
  const [stateError, setStateError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [dailyPnl, setDailyPnl] = useState<DailyPnl[]>([]);
  const [dailyPnlError, setDailyPnlError] = useState<unknown>(null);
  const [isDark, setIsDark] = useState(false);

  // VaR estimate
  const [varEstimate, setVarEstimate] = useState<VarEstimate | null>(null);
  const [varState, setVarState] = useState<DataState>('ok');

  // Risk audit trail (breaker transitions, resets, kill switch)
  const [events, setEvents] = useState<RiskEvent[]>([]);
  const [eventsError, setEventsError] = useState<unknown>(null);

  // Live status (polled); null kill switch means the state is unknown
  const [liveStatus, setLiveStatus] = useState<LiveStatus | null>(null);

  // Risk config form state: empty until loaded, never pre-filled with defaults
  const [riskConfig, setRiskConfig] = useState<RiskConfig | null>(null);
  const [riskConfigError, setRiskConfigError] = useState<unknown>(null);
  const [limitsForm, setLimitsForm] = useState<RiskLimitsForm | null>(null);
  const limitsBaselineRef = useRef<RiskLimitsForm | null>(null);
  const limitsDirtyRef = useRef(false);
  const [killSwitchActive, setKillSwitchActive] = useState<boolean | null>(null);
  const [killSwitchLoading, setKillSwitchLoading] = useState(false);
  const [killSwitchRemainder, setKillSwitchRemainder] = useState<KillSwitchRemainder | null>(null);
  // null until the sessions list has been read while the switch is latched
  const [serverRemaining, setServerRemaining] = useState<number | null>(null);

  // null until /api/system/config says which account the page reports on
  const sourceRef = useRef<string | null>(null);

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains('dark'));
  }, []);

  // Consolidated data loader
  const loadRiskData = useCallback(async () => {
    let source: string | null = null;
    try {
      const cfg = await fetchApi<{ trading_mode: string }>('/api/system/config');
      source = cfg.trading_mode === 'live' ? 'live' : 'paper';
    } catch (err) {
      logger.warn('Risk', 'Failed to fetch trading mode', err);
      setStateError(err);
      setDailyPnlError(err);
      setVarState(stateFromError(err));
    }
    sourceRef.current = source;
    const qs = `?source=${source}`;
    const sourced = source === null ? [] : [
      fetchApi<ApiRiskStatus>(`/api/risk/status${qs}`)
        .then((data) => { setState(mapApiRiskStatus(data)); setStateError(null); })
        .catch((err) => { logger.warn('Risk', 'Failed to fetch risk status', err); setStateError(err); }),
      fetchApi<VarEstimate>(`/api/risk/var${qs}`)
        .then((data) => { setVarEstimate(data); setVarState('ok'); })
        .catch((err) => { logger.warn('Risk', 'Failed to fetch VaR', err); setVarState(stateFromError(err)); }),
      fetchApi<DailyPnl[]>(`/api/portfolio/daily-pnl${qs}`)
        .then((data) => { setDailyPnl(data); setDailyPnlError(null); })
        .catch((err) => { logger.warn('Risk', 'Failed to fetch daily PnL', err); setDailyPnlError(err); }),
    ];

    await Promise.all([
      ...sourced,
      fetchApi<RiskEvent[]>(EVENTS_PATH)
        .then((data) => { setEvents(data); setEventsError(null); })
        .catch((err) => { logger.warn('Risk', 'Failed to fetch risk events', err); setEventsError(err); }),
      fetchApi<RiskConfig>('/api/risk/config')
        .then((riskCfg) => {
          setRiskConfig(riskCfg);
          if (!limitsDirtyRef.current) {
            const form = riskLimitsToForm(riskCfg);
            limitsBaselineRef.current = form;
            setLimitsForm(form);
          }
          setKillSwitchActive(riskCfg.kill_switch_active);
          setRiskConfigError(null);
        })
        .catch((err) => {
          logger.warn('Risk', 'Failed to fetch risk config', err);
          setRiskConfig(null);
          setRiskConfigError(err);
          setKillSwitchActive(null);
        }),
    ]);

    setLoading(false);
  }, []);

  // Initial load + 30s polling
  useEffect(() => {
    loadRiskData();
    const interval = setInterval(loadRiskData, 30_000);
    return () => clearInterval(interval);
  }, [loadRiskData]);

  // Poll live status + risk status every 10s for responsive kill switch & drawdown
  useEffect(() => {
    const fetchLiveStatus = () => {
      const source = sourceRef.current;
      fetchApi<LiveStatus>('/api/risk/live-status')
        .then((data) => {
          setLiveStatus(data);
          setKillSwitchActive(data.kill_switch_active);
          if (data.kill_switch_active === false) {
            setKillSwitchRemainder(null);
            setServerRemaining(null);
          } else if (data.kill_switch_active === true) {
            fetchApi<SessionRemainder[]>('/api/trading/sessions')
              .then((sessions) => setServerRemaining(remainingFromSessions(sessions)))
              .catch((err) => logger.warn('Risk', 'Failed to fetch sessions for the kill switch remainder', err));
          }
        })
        .catch((err) => {
          logger.warn('Risk', 'Failed to fetch live status', err);
          setLiveStatus(null);
          setKillSwitchActive(null);
        });
      if (source === null) return;
      fetchApi<ApiRiskStatus>(`/api/risk/status?source=${source}`)
        .then((data) => { setState(mapApiRiskStatus(data)); setStateError(null); })
        .catch((err) => { logger.warn('Risk', 'Failed to refresh risk status', err); setStateError(err); });
    };
    fetchLiveStatus();
    const interval = setInterval(fetchLiveStatus, 10_000);
    return () => clearInterval(interval);
  }, []);

  const setLimit = (field: RiskLimitField, value: string) => {
    limitsDirtyRef.current = true;
    setLimitsForm((prev) => (prev ? { ...prev, [field]: value } : prev));
  };

  const handleSaveRiskConfig = async () => {
    const baseline = limitsBaselineRef.current;
    if (!baseline || !limitsForm) return;
    const result = buildRiskLimitsUpdate(baseline, limitsForm);
    if (!result.ok) {
      toast('error', `Invalid value for ${result.field.replace(/_/g, ' ')}`);
      return;
    }
    if (Object.keys(result.update).length === 0) {
      toast('info', 'No changes to save');
      return;
    }
    try {
      await fetchApi('/api/risk/config', {
        method: 'PUT',
        body: JSON.stringify(result.update),
      });
      limitsDirtyRef.current = false;
      limitsBaselineRef.current = limitsForm;
      toast('success', 'Risk configuration saved');
      void loadRiskData();
    } catch (err) {
      logger.error('Risk', 'Failed to save risk config', err);
      toast('error', describeApiError(err, 'Failed to save risk configuration'));
    }
  };

  // Unknown state only offers activation: it is the fail-safe direction.
  const handleKillSwitch = async () => {
    let releaseReason = '';
    if (killSwitchActive !== true) {
      if (!confirm('ACTIVATE KILL SWITCH?\n\nThis will immediately stop ALL trading sessions. Are you sure?')) return;
    } else {
      if (window.prompt('Type RELEASE to allow trading to resume') !== 'RELEASE') return;
      releaseReason = (window.prompt('Reason for releasing the kill switch (audited)') ?? '').trim();
      if (releaseReason.length < 3) {
        toast('error', 'A reason of at least 3 characters is required to release the kill switch');
        return;
      }
    }
    setKillSwitchLoading(true);
    try {
      if (killSwitchActive === true) {
        await fetchApi('/api/trading/kill-switch', {
          method: 'DELETE',
          body: JSON.stringify({ confirm: 'RELEASE', reason: releaseReason }),
        });
        setKillSwitchActive(false);
        setKillSwitchRemainder(null);
        setServerRemaining(null);
        toast('success', 'Kill switch deactivated');
        logger.warn('Risk', 'Kill switch deactivated');
      } else {
        const res = await fetchApi<KillSwitchResponse>('/api/trading/kill-switch', { method: 'POST' });
        const remainder = remainderFrom({ ...res }, true);
        setKillSwitchActive(true);
        setKillSwitchRemainder(remainder);
        setServerRemaining(null);
        if (remainder) {
          toast('error', `Kill switch ${latchedText(remainder.positionsRemaining)}`);
        } else {
          toast('warning', 'Kill switch ACTIVATED — all positions closed and trading halted');
        }
        logger.warn('Risk', 'KILL SWITCH ACTIVATED', res);
      }
    } catch (err) {
      const detail = parseErrorDetail(err);
      if (killSwitchActive !== true && detail?.code === 'kill_switch_not_persisted') {
        setKillSwitchActive(true);
        setKillSwitchRemainder(remainderFrom(detail, false));
        setServerRemaining(null);
        toast('error', detail.message || 'Kill switch latched but not persisted');
        logger.error('Risk', 'Kill switch latched but not persisted', err);
      } else {
        logger.error('Risk', 'Failed to toggle kill switch', err);
        toast('error', describeApiError(err, 'Failed to toggle kill switch'));
      }
    } finally {
      setKillSwitchLoading(false);
    }
  };

  const positionsOpen = serverRemaining ?? killSwitchRemainder?.positionsRemaining ?? 0;
  const activationFailures = positionsOpen > 0 ? killSwitchRemainder?.flattenFailures ?? [] : [];

  return (
    <div className="flex flex-col gap-6">
      {/* Live status badge */}
      {!loading && (
        <div className="flex items-center gap-4">
          <div className={cn(
            'inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium',
            killSwitchActive === true
              ? 'bg-status-error/10 text-status-error border border-status-error/30'
              : killSwitchActive === false
                ? 'bg-status-success/10 text-status-success border border-status-success/30'
                : 'bg-status-warning/10 text-status-warning border border-status-warning/30',
          )}>
            <Activity className="h-3.5 w-3.5" />
            {killSwitchActive === true
              ? 'Kill Switch Active'
              : killSwitchActive === false
                ? 'System Normal'
                : 'Kill switch: UNKNOWN'}
          </div>
          {liveStatus?.running_sessions != null && (
            <span className="text-xs text-text-muted">
              {liveStatus.running_sessions} running session{liveStatus.running_sessions !== 1 ? 's' : ''}
            </span>
          )}
        </div>
      )}

      {/* Kill switch banner */}
      {killSwitchActive === true && (
        <div className="rounded-xl border-2 border-status-error bg-status-error/10 p-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <OctagonX className="h-6 w-6 text-status-error" />
            <div>
              <p className="text-sm font-semibold text-status-error">
                {positionsOpen > 0 ? latchedText(positionsOpen) : 'Kill Switch Active'}
              </p>
              <p className="text-xs text-text-muted">All trading is halted. No new sessions can start.</p>
              {positionsOpen > 0 && (
                <p className="text-xs text-status-error">Those positions are still open: check the sessions.</p>
              )}
              {activationFailures.length > 0 && (
                <p className="text-xs text-text-muted">At activation:</p>
              )}
              {activationFailures.map((f, i) => (
                <p key={`${i}:${f}`} className="text-xs text-text-muted">{f}</p>
              ))}
              {killSwitchRemainder && !killSwitchRemainder.persisted && (
                <p className="text-xs text-status-error">
                  Not persisted — the latch is in memory only and is lost on restart.
                </p>
              )}
            </div>
          </div>
          <Button variant="outline" size="sm" onClick={handleKillSwitch} loading={killSwitchLoading}>
            Release Kill Switch
          </Button>
        </div>
      )}

      {/* Circuit breaker status cards */}
      {loading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} />)}
        </div>
      ) : stateError || !state ? (
        <DataStateErrorCard
          error={stateError}
          fallback="Failed to load risk status"
          onRetry={() => {
            setLoading(true);
            setStateError(null);
            void loadRiskData();
          }}
        />
      ) : (
        <>
          {state.circuitBreakers === null ? (
            <div className="rounded-xl border border-border-default bg-bg-secondary p-4 md:p-5">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-text-primary">Circuit breakers</h3>
                <DataStateBadge state="unavailable" reason="risk service not running" />
              </div>
              <p className="mt-2 text-xs text-text-muted">
                The engine&apos;s risk service is not available, so no tier status is shown.
              </p>
            </div>
          ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {state.circuitBreakers.map((cb) => (
              <div
                key={cb.tier}
                className={cn(
                  'rounded-xl border p-4 md:p-5',
                  cb.borderColor,
                  cb.bgColor,
                )}
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold uppercase tracking-wider text-text-muted">
                    {cb.tier}
                  </span>
                  <cb.icon className={cn('h-5 w-5', cb.color)} aria-hidden="true" />
                </div>
                <h3 className="mt-2 text-sm font-semibold text-text-primary">
                  {cb.label}
                </h3>
                <p className="mt-1 text-xs text-text-muted">{cb.threshold}</p>
                <div className="mt-3 flex items-center justify-between">
                  <span className="text-xs text-text-muted">Current</span>
                  <span className={cn('text-lg font-bold font-display', cb.color)}>
                    {cb.currentValue}
                  </span>
                </div>
                <div className="mt-2">
                  <span
                    className={cn(
                      'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium',
                      cb.bgColor,
                      cb.color,
                    )}
                  >
                    {cb.status}
                  </span>
                </div>
                {cb.activeSince && (
                  <p className="mt-2 text-xs text-text-muted">Since {fmtTime(cb.activeSince)}</p>
                )}
                {cb.requiresManualReview && (
                  <p className="mt-1 text-xs font-medium text-status-error">Manual review required</p>
                )}
              </div>
            ))}
          </div>
          )}

          {state.restrictions && (
            <div
              data-testid="risk-restrictions"
              className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl border border-border-default bg-bg-secondary px-4 py-3 text-xs"
            >
              <span className={cn('font-medium', state.restrictions.can_open_new_trades ? 'text-status-success' : 'text-status-error')}>
                {state.restrictions.can_open_new_trades ? 'New entries allowed' : 'New entries blocked'}
              </span>
              <span className="text-text-muted">Size multiplier x{state.restrictions.size_multiplier}</span>
              <span className="text-text-muted">
                Losing streak {state.consecutiveLosses} / {state.maxConsecutiveLosses}
              </span>
              {state.restrictions.daily_halt_until && (
                <span className="text-status-error">Daily halt until {fmtTime(state.restrictions.daily_halt_until)}</span>
              )}
              {state.restrictions.consecutive_pause_until && (
                <span className="text-status-error">Loss pause until {fmtTime(state.restrictions.consecutive_pause_until)}</span>
              )}
            </div>
          )}

          {/* Gauges row */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* Drawdown gauge */}
            <DataCard title="Current Drawdown" description="Real-time portfolio drawdown from peak">
              {(() => {
                const drawdownPct = state.maxDrawdown > 0 ? (state.drawdown / state.maxDrawdown) * 100 : 0;
                return (
                  <>
                    <div className="relative h-40">
                      <ResponsiveContainer width="100%" height="100%">
                        <RadialBarChart
                          cx="50%"
                          cy="80%"
                          innerRadius="70%"
                          outerRadius="100%"
                          startAngle={180}
                          endAngle={0}
                          data={[{ value: drawdownPct }]}
                        >
                          <RadialBar
                            dataKey="value"
                            cornerRadius={4}
                            background={{ fill: isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)' }}
                            fill={drawdownPct < 40 ? '#22c55e' : drawdownPct < 70 ? '#f59e0b' : '#ef4444'}
                          />
                        </RadialBarChart>
                      </ResponsiveContainer>
                      <div className="absolute bottom-0 left-0 right-0 flex flex-col items-center">
                        <span className={cn(
                          'text-2xl font-bold font-display',
                          drawdownPct < 40 ? 'text-status-success' : drawdownPct < 70 ? 'text-status-warning' : 'text-status-error',
                        )}>
                          {state.drawdown.toFixed(1)}%
                        </span>
                        <span className="text-xs text-text-muted">of {state.maxDrawdown}% limit</span>
                      </div>
                    </div>
                    <div className="mt-4 space-y-2">
                      <div className="flex justify-between text-xs">
                        <span className="text-text-muted">Current Drawdown</span>
                        <span className={cn('font-medium', drawdownPct < 40 ? 'text-status-success' : drawdownPct < 70 ? 'text-status-warning' : 'text-status-error')}>{state.drawdown}%</span>
                      </div>
                      <div className="h-3 rounded-full bg-bg-tertiary overflow-hidden">
                        <div
                          className={cn('h-full rounded-full transition-all', drawdownPct < 40 ? 'bg-status-success' : drawdownPct < 70 ? 'bg-status-warning' : 'bg-status-error')}
                          style={{ width: `${drawdownPct}%` }}
                        />
                      </div>
                      <div className="flex justify-between text-xs text-text-light">
                        <span>0%</span>
                        <span>{state.maxDrawdown}% (Kill switch)</span>
                      </div>
                    </div>
                  </>
                );
              })()}
            </DataCard>

            {/* Daily loss tracker */}
            <DataCard title="Daily Loss Tracker" description="Cumulative losses today">
              {dailyPnlError !== null ? (
                <div className="flex h-48 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border-default bg-bg-secondary">
                  <DataStateBadge state={stateFromError(dailyPnlError)} />
                  <p className="text-sm text-text-muted">{describeApiError(dailyPnlError, 'Daily PnL could not be loaded')}</p>
                </div>
              ) : dailyPnl.length === 0 ? (
                <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
                  <p className="text-sm text-text-muted">No data</p>
                </div>
              ) : (
                <div className="h-48">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={dailyPnl} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke={isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)'} vertical={false} />
                      <XAxis
                        dataKey="date"
                        tick={{ fontSize: 11, fill: isDark ? 'rgba(255,255,255,0.44)' : '#475569' }}
                        axisLine={false}
                        tickLine={false}
                        minTickGap={20}
                      />
                      <YAxis
                        tickFormatter={(v: number) => `$${v >= 0 ? '' : '-'}${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 0 })}`}
                        tick={{ fontSize: 11, fill: isDark ? 'rgba(255,255,255,0.44)' : '#475569' }}
                        axisLine={false}
                        tickLine={false}
                        width={52}
                      />
                      <Tooltip
                        contentStyle={{
                          background: isDark ? '#1e2433' : '#ffffff',
                          border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : '#e2e8f0'}`,
                          borderRadius: '8px',
                          fontSize: '12px',
                        }}
                        formatter={(raw) => { const v = Number(raw); return [
                          `${v >= 0 ? '+' : ''}$${v.toLocaleString('en-US', { minimumFractionDigits: 2 })}`,
                          'PnL',
                        ]; }}
                      />
                      <Bar dataKey="pnl" radius={[3, 3, 0, 0]}>
                        {dailyPnl.map((entry, index) => (
                          <Cell
                            key={`cell-${index}`}
                            fill={entry.pnl >= 0 ? '#22c55e' : '#ef4444'}
                          />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
              <div className="mt-4 space-y-2">
                {[
                  {
                    label: 'Current daily loss',
                    value: state.dailyLoss === null ? 'unknown' : `-${fmtUsd(state.dailyLoss)}`,
                    color: 'text-status-error',
                  },
                ].map((row) => (
                  <div key={row.label} className="flex justify-between text-xs">
                    <span className="text-text-muted">{row.label}</span>
                    <span className={cn('font-medium', row.color)}>{row.value}</span>
                  </div>
                ))}
              </div>
            </DataCard>
          </div>

          {/* Value at Risk card */}
          {varState !== 'ok' ? (
            <DataCard title="Value at Risk" description="Historical simulation">
              <div className="flex items-center gap-3">
                <DataStateBadge state={varState} />
                <p className="text-sm text-text-muted">VaR could not be computed.</p>
              </div>
            </DataCard>
          ) : varEstimate && varEstimate.sample_days < MIN_VAR_SAMPLE_DAYS ? (
            <DataCard title="Value at Risk" description={`Method: ${varEstimate.calculation_method.replace(/_/g, ' ')}`}>
              <p className="text-sm text-text-muted">
                Insufficient history ({varEstimate.sample_days} day{varEstimate.sample_days !== 1 ? 's' : ''}, need {MIN_VAR_SAMPLE_DAYS})
              </p>
            </DataCard>
          ) : varEstimate && (
            <DataCard title="Value at Risk" description={`Method: ${varEstimate.calculation_method.replace(/_/g, ' ')} over ${varEstimate.sample_days} days`}>
              <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
                <div>
                  <p className="text-xs text-text-muted">VaR 95%</p>
                  <p className="mt-1 text-lg font-bold text-status-warning font-display">{fmtUsd(varEstimate.var_95)}</p>
                </div>
                <div>
                  <p className="text-xs text-text-muted">VaR 99%</p>
                  <p className="mt-1 text-lg font-bold text-status-error font-display">{fmtUsd(varEstimate.var_99)}</p>
                </div>
                <div>
                  <p className="text-xs text-text-muted">CVaR 95%</p>
                  <p className="mt-1 text-lg font-bold text-status-error font-display">{fmtUsd(varEstimate.cvar_95)}</p>
                </div>
                <div>
                  <p className="text-xs text-text-muted">Portfolio Value</p>
                  <p className="mt-1 text-lg font-bold text-text-primary font-display">{fmtUsd(varEstimate.portfolio_value)}</p>
                </div>
              </div>
            </DataCard>
          )}
        </>
      )}

      {/* Risk audit trail */}
      <DataCard title="Risk Events" description="Breaker transitions, resets and kill-switch changes">
        {eventsError !== null ? (
          <div className="flex items-center gap-3">
            <DataStateBadge state={stateFromError(eventsError)} />
            <p className="text-sm text-text-muted">{describeApiError(eventsError, 'Risk events could not be loaded')}</p>
          </div>
        ) : events.length === 0 ? (
          <p className="text-sm text-text-muted">No risk events recorded</p>
        ) : (
          <ul className="divide-y divide-border-default text-xs">
            {events.map((e, i) => (
              <li key={e.id ?? i} className="flex flex-wrap gap-x-3 py-2">
                <span className="text-text-muted">{fmtTime(e.ts)}</span>
                <span className="font-medium text-text-primary">{e.kind}</span>
                {e.tier !== null && <span className="text-text-muted">tier {e.tier}</span>}
                <span className="text-text-muted">{e.actor}</span>
                <span className="text-text-primary">{e.reason}</span>
              </li>
            ))}
          </ul>
        )}
      </DataCard>

      {/* Editable risk limits */}
      <DataCard title="Risk Limits" description="Configure global risk management parameters">
        {riskConfigError !== null && (
          <div className="mb-4">
            <DataStateErrorCard
              error={riskConfigError}
              fallback="Failed to load risk limits"
              onRetry={loadRiskData}
            />
          </div>
        )}
        {riskConfig !== null && limitsForm !== null && (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          <Input
            label="Max Position Size (%)"
            type="number"
            value={limitsForm.max_position_pct}
            onChange={(e) => setLimit('max_position_pct', e.target.value)}
            hint="Maximum % of portfolio per position"
          />
          <Input
            label="Max Risk Per Trade (%)"
            type="number"
            value={limitsForm.max_risk_per_trade}
            onChange={(e) => setLimit('max_risk_per_trade', e.target.value)}
            hint="Maximum risk per individual trade"
          />
          <Input
            label="Max Portfolio Heat (%)"
            type="number"
            value={limitsForm.max_portfolio_heat}
            onChange={(e) => setLimit('max_portfolio_heat', e.target.value)}
            hint="Sum of all position risks as % of portfolio"
          />
          <Input
            label="Max Daily Loss (%)"
            type="number"
            value={limitsForm.max_daily_loss_pct}
            onChange={(e) => setLimit('max_daily_loss_pct', e.target.value)}
            hint="Daily loss limit before trading halt"
          />
          <Input
            label="Max Drawdown (%)"
            type="number"
            value={limitsForm.max_drawdown_pct}
            onChange={(e) => setLimit('max_drawdown_pct', e.target.value)}
            hint="Maximum drawdown before kill switch"
          />
          <Input
            label="Max Concurrent Positions"
            type="number"
            value={limitsForm.max_concurrent_positions}
            onChange={(e) => setLimit('max_concurrent_positions', e.target.value)}
            hint="Maximum number of open positions"
          />
        </div>
        )}
        <div className="mt-4 flex items-center justify-end">
          <div className="flex items-center gap-3">
            <Button variant="primary" onClick={handleSaveRiskConfig} disabled={riskConfig === null || limitsForm === null}>
              Save Risk Limits
            </Button>
            {killSwitchActive !== true && (
              <Button variant="danger" onClick={handleKillSwitch} loading={killSwitchLoading}>
                <OctagonX className="h-4 w-4" />
                Kill Switch
              </Button>
            )}
          </div>
        </div>
      </DataCard>
    </div>
  );
}
