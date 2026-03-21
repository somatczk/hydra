'use client';

import { useEffect, useState } from 'react';
import { ShieldCheck, ShieldAlert, AlertTriangle, Ban, OctagonX, Activity } from 'lucide-react';
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
import { ErrorCard } from '@/components/ui/ErrorCard';
import { logger } from '@/lib/logger';

/* ---------- Types ---------- */

interface CircuitBreaker {
  tier: string;
  label: string;
  threshold: string;
  currentValue: string;
  status: 'Normal' | 'Warning' | 'Alert' | 'Tripped';
  color: string;
  bgColor: string;
  borderColor: string;
  icon: typeof ShieldCheck;
}

interface ApiRiskStatus {
  current_drawdown: number;
  max_drawdown_limit: number;
  daily_loss: number;
  daily_loss_limit: number;
  circuit_breakers: Array<{
    tier: number;
    label: string;
    threshold: string;
    current_value: number;
    status: string;
  }>;
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
}

interface LiveStatus {
  kill_switch_active: boolean;
  running_sessions: number;
  circuit_breaker_restrictions: Record<string, unknown> | null;
}

/* ---------- Helpers ---------- */

function statusToColors(s: string) {
  switch (s) {
    case 'Warning':
      return { color: 'text-status-warning', bgColor: 'bg-status-warning/10', borderColor: 'border-status-warning/30', icon: AlertTriangle };
    case 'Alert':
    case 'Tripped':
      return { color: 'text-status-error', bgColor: 'bg-status-error/10', borderColor: 'border-status-error/30', icon: Ban };
    default:
      return { color: 'text-status-success', bgColor: 'bg-status-success/10', borderColor: 'border-status-success/30', icon: ShieldCheck };
  }
}

interface RiskEvent {
  time: string;
  message: string;
  severity: 'warning' | 'info';
}

interface RiskState {
  circuitBreakers: CircuitBreaker[];
  drawdown: number;
  maxDrawdown: number;
  dailyLoss: number;
}

function mapApiRiskStatus(data: ApiRiskStatus): RiskState {
  const breakers: CircuitBreaker[] = data.circuit_breakers.map((cb) => {
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
      status: cb.status as CircuitBreaker['status'],
      ...finalColors,
    };
  });
  return {
    circuitBreakers: breakers,
    drawdown: data.current_drawdown,
    maxDrawdown: data.max_drawdown_limit,
    dailyLoss: data.daily_loss,
  };
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
  const [stateError, setStateError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [dailyPnl, setDailyPnl] = useState<DailyPnl[]>([]);
  const [isDark, setIsDark] = useState(false);
  const [riskEvents] = useState<RiskEvent[]>([
    { time: '14:32', message: 'Position stop-loss triggered on BTC/USDT short at $68,180', severity: 'warning' },
    { time: '11:15', message: 'Strategy cooldown activated for Breakout Scanner after 3 consecutive losses', severity: 'warning' },
    { time: '09:00', message: 'Daily risk limits reset. All circuit breakers cleared.', severity: 'info' },
  ]);

  // VaR estimate
  const [varEstimate, setVarEstimate] = useState<VarEstimate | null>(null);

  // Live status (polled)
  const [liveStatus, setLiveStatus] = useState<LiveStatus | null>(null);

  // Risk config form state
  const [riskConfig, setRiskConfig] = useState<RiskConfig | null>(null);
  const [maxPositionPct, setMaxPositionPct] = useState('10');
  const [maxRiskPerTrade, setMaxRiskPerTrade] = useState('2');
  const [maxPortfolioHeat, setMaxPortfolioHeat] = useState('6');
  const [maxDailyLossPct, setMaxDailyLossPct] = useState('3');
  const [maxDrawdownPct, setMaxDrawdownPct] = useState('15');
  const [maxConcurrentPositions, setMaxConcurrentPositions] = useState('10');
  const [killSwitchActive, setKillSwitchActive] = useState(false);
  const [killSwitchLoading, setKillSwitchLoading] = useState(false);

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains('dark'));
  }, []);

  // Fetch risk status
  useEffect(() => {
    fetchApi<ApiRiskStatus>('/api/risk/status')
      .then((data) => { setState(mapApiRiskStatus(data)); setStateError(false); })
      .catch((err) => { logger.warn('Risk', 'Failed to fetch risk status', err); setStateError(true); })
      .finally(() => setLoading(false));
  }, []);

  // Fetch VaR estimate
  useEffect(() => {
    fetchApi<VarEstimate>('/api/risk/var')
      .then((data) => setVarEstimate(data))
      .catch((err) => { logger.warn('Risk', 'Failed to fetch VaR estimate', err); });
  }, []);

  // Poll live status every 10s
  useEffect(() => {
    const fetchLiveStatus = () => {
      fetchApi<LiveStatus>('/api/risk/live-status')
        .then((data) => {
          setLiveStatus(data);
          setKillSwitchActive(data.kill_switch_active);
        })
        .catch((err) => { logger.warn('Risk', 'Failed to fetch live status', err); });
    };
    fetchLiveStatus();
    const interval = setInterval(fetchLiveStatus, 10_000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    fetchApi<{ trading_mode: string }>('/api/system/config')
      .catch(() => ({ trading_mode: 'paper' }))
      .then((cfg) => {
        const source = cfg.trading_mode === 'live' ? 'live' : 'paper';
        return fetchApi<DailyPnl[]>(`/api/portfolio/daily-pnl?source=${source}`);
      })
      .then((data) => setDailyPnl(data))
      .catch((err) => { logger.warn('Risk', 'Failed to fetch daily PnL', err); });
  }, []);

  // Fetch risk config from DB
  useEffect(() => {
    fetchApi<RiskConfig>('/api/risk/config')
      .then((cfg) => {
        setRiskConfig(cfg);
        setMaxPositionPct((cfg.max_position_pct * 100).toFixed(0));
        setMaxRiskPerTrade((cfg.max_risk_per_trade * 100).toFixed(0));
        setMaxPortfolioHeat((cfg.max_portfolio_heat * 100).toFixed(0));
        setMaxDailyLossPct((cfg.max_daily_loss_pct * 100).toFixed(0));
        setMaxDrawdownPct((cfg.max_drawdown_pct * 100).toFixed(0));
        setMaxConcurrentPositions(cfg.max_concurrent_positions.toString());
        setKillSwitchActive(cfg.kill_switch_active);
      })
      .catch((err) => { logger.warn('Risk', 'Failed to fetch risk config', err); });
  }, []);

  const handleSaveRiskConfig = async () => {
    try {
      await fetchApi('/api/risk/config', {
        method: 'PUT',
        body: JSON.stringify({
          max_position_pct: parseFloat(maxPositionPct) / 100,
          max_risk_per_trade: parseFloat(maxRiskPerTrade) / 100,
          max_portfolio_heat: parseFloat(maxPortfolioHeat) / 100,
          max_daily_loss_pct: parseFloat(maxDailyLossPct) / 100,
          max_drawdown_pct: parseFloat(maxDrawdownPct) / 100,
          max_concurrent_positions: parseInt(maxConcurrentPositions, 10),
        }),
      });
      toast('success', 'Risk configuration saved');
    } catch (err) {
      logger.error('Risk', 'Failed to save risk config', err);
      toast('error', 'Failed to save risk configuration');
    }
  };

  const handleKillSwitch = async () => {
    if (!killSwitchActive) {
      if (!confirm('ACTIVATE KILL SWITCH?\n\nThis will immediately stop ALL trading sessions. Are you sure?')) return;
    }
    setKillSwitchLoading(true);
    try {
      if (killSwitchActive) {
        await fetchApi('/api/trading/kill-switch', { method: 'DELETE' });
        setKillSwitchActive(false);
        toast('success', 'Kill switch deactivated');
        logger.warn('Risk', 'Kill switch deactivated');
      } else {
        await fetchApi('/api/trading/kill-switch', { method: 'POST' });
        setKillSwitchActive(true);
        toast('warning', 'Kill switch ACTIVATED — all trading halted');
        logger.warn('Risk', 'KILL SWITCH ACTIVATED');
      }
    } catch (err) {
      logger.error('Risk', 'Failed to toggle kill switch', err);
      toast('error', 'Failed to toggle kill switch');
    } finally {
      setKillSwitchLoading(false);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      {/* Live status badge */}
      {liveStatus && (
        <div className="flex items-center gap-4">
          <div className={cn(
            'inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium',
            liveStatus.kill_switch_active
              ? 'bg-status-error/10 text-status-error border border-status-error/30'
              : 'bg-status-success/10 text-status-success border border-status-success/30',
          )}>
            <Activity className="h-3.5 w-3.5" />
            {liveStatus.kill_switch_active ? 'Kill Switch Active' : 'System Normal'}
          </div>
          <span className="text-xs text-text-muted">
            {liveStatus.running_sessions} running session{liveStatus.running_sessions !== 1 ? 's' : ''}
          </span>
        </div>
      )}

      {/* Kill switch banner */}
      {killSwitchActive && (
        <div className="rounded-xl border-2 border-status-error bg-status-error/10 p-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <OctagonX className="h-6 w-6 text-status-error" />
            <div>
              <p className="text-sm font-semibold text-status-error">Kill Switch Active</p>
              <p className="text-xs text-text-muted">All trading is halted. No new sessions can start.</p>
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
        <ErrorCard
          message="Failed to load risk status"
          onRetry={() => {
            setLoading(true);
            setStateError(false);
            fetchApi<ApiRiskStatus>('/api/risk/status')
              .then((data) => { setState(mapApiRiskStatus(data)); setStateError(false); })
              .catch(() => setStateError(true))
              .finally(() => setLoading(false));
          }}
        />
      ) : (
        <>
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
              </div>
            ))}
          </div>

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
                        <span className="font-medium text-status-warning">{state.drawdown}%</span>
                      </div>
                      <div className="h-3 rounded-full bg-bg-tertiary overflow-hidden">
                        <div
                          className="h-full rounded-full bg-status-warning transition-all"
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
              {dailyPnl.length === 0 ? (
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
                        formatter={(v: number) => [
                          `${v >= 0 ? '+' : ''}$${v.toLocaleString('en-US', { minimumFractionDigits: 2 })}`,
                          'PnL',
                        ]}
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
                  { label: 'Max daily loss', value: `-${fmtUsd(state.dailyLoss)}`, color: 'text-status-error' },
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
          {varEstimate && (
            <DataCard title="Value at Risk" description={`Method: ${varEstimate.calculation_method.replace(/_/g, ' ')}`}>
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

      {/* Editable risk limits */}
      <DataCard title="Risk Limits" description="Configure global risk management parameters">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          <Input
            label="Max Position Size (%)"
            type="number"
            value={maxPositionPct}
            onChange={(e) => setMaxPositionPct(e.target.value)}
            hint="Maximum % of portfolio per position"
          />
          <Input
            label="Max Risk Per Trade (%)"
            type="number"
            value={maxRiskPerTrade}
            onChange={(e) => setMaxRiskPerTrade(e.target.value)}
            hint="Maximum risk per individual trade"
          />
          <Input
            label="Max Portfolio Heat (%)"
            type="number"
            value={maxPortfolioHeat}
            onChange={(e) => setMaxPortfolioHeat(e.target.value)}
            hint="Sum of all position risks as % of portfolio"
          />
          <Input
            label="Max Daily Loss (%)"
            type="number"
            value={maxDailyLossPct}
            onChange={(e) => setMaxDailyLossPct(e.target.value)}
            hint="Daily loss limit before trading halt"
          />
          <Input
            label="Max Drawdown (%)"
            type="number"
            value={maxDrawdownPct}
            onChange={(e) => setMaxDrawdownPct(e.target.value)}
            hint="Maximum drawdown before kill switch"
          />
          <Input
            label="Max Concurrent Positions"
            type="number"
            value={maxConcurrentPositions}
            onChange={(e) => setMaxConcurrentPositions(e.target.value)}
            hint="Maximum number of open positions"
          />
        </div>
        <div className="mt-4 flex items-center justify-end">
          <div className="flex items-center gap-3">
            <Button variant="primary" onClick={handleSaveRiskConfig}>
              Save Risk Limits
            </Button>
            {!killSwitchActive && (
              <Button variant="danger" onClick={handleKillSwitch} loading={killSwitchLoading}>
                <OctagonX className="h-4 w-4" />
                Kill Switch
              </Button>
            )}
          </div>
        </div>
      </DataCard>

      {/* Risk alerts */}
      <DataCard title="Recent Risk Events" description="Risk management actions taken today">
        <div className="space-y-3">
          {riskEvents.map((event, idx) => (
            <div
              key={idx}
              className="flex items-start gap-3 rounded-lg border border-border-default bg-bg-secondary p-3"
            >
              <ShieldAlert
                className={cn(
                  'mt-0.5 h-4 w-4 shrink-0',
                  event.severity === 'warning' ? 'text-status-warning' : 'text-status-info',
                )}
                aria-hidden="true"
              />
              <div className="flex-1">
                <p className="text-sm text-text-primary">{event.message}</p>
                <p className="mt-0.5 text-xs text-text-light">{event.time}</p>
              </div>
            </div>
          ))}
        </div>
      </DataCard>
    </div>
  );
}
