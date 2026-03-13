'use client';

import { useEffect, useState } from 'react';
import { ShieldCheck, ShieldAlert, AlertTriangle, Ban, Activity, TrendingDown } from 'lucide-react';
import { DataCard } from '@/components/ui/DataCard';
import { cn } from '@/components/ui/cn';
import { fetchApi } from '@/lib/api';

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

/* ---------- Placeholder data ---------- */

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

const placeholderBreakers: CircuitBreaker[] = [
  { tier: 'Tier 1', label: 'Position Level', threshold: '2% per position', currentValue: '0.8%', status: 'Normal', color: 'text-status-success', bgColor: 'bg-status-success/10', borderColor: 'border-status-success/30', icon: ShieldCheck },
  { tier: 'Tier 2', label: 'Strategy Level', threshold: '5% daily loss per strategy', currentValue: '1.2%', status: 'Normal', color: 'text-status-success', bgColor: 'bg-status-success/10', borderColor: 'border-status-success/30', icon: ShieldCheck },
  { tier: 'Tier 3', label: 'Portfolio Level', threshold: '10% daily portfolio loss', currentValue: '3.8%', status: 'Warning', color: 'text-status-warning', bgColor: 'bg-status-warning/10', borderColor: 'border-status-warning/30', icon: AlertTriangle },
  { tier: 'Tier 4', label: 'System Kill Switch', threshold: '15% daily loss - halt all trading', currentValue: '3.8%', status: 'Normal', color: 'text-status-error', bgColor: 'bg-status-error/10', borderColor: 'border-status-error/30', icon: Ban },
];

interface RiskState {
  circuitBreakers: CircuitBreaker[];
  drawdown: number;
  maxDrawdown: number;
  dailyLoss: number;
}

const placeholderState: RiskState = {
  circuitBreakers: placeholderBreakers,
  drawdown: 4.2,
  maxDrawdown: 15.0,
  dailyLoss: 48.90,
};

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

/* ---------- Page ---------- */

export default function RiskPage() {
  const [state, setState] = useState<RiskState>(placeholderState);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchApi<ApiRiskStatus>('/api/risk/status')
      .then((data) => setState(mapApiRiskStatus(data)))
      .catch(() => { /* keep placeholder */ })
      .finally(() => setLoading(false));
  }, []);

  const { circuitBreakers, drawdown, maxDrawdown } = state;
  const drawdownPct = maxDrawdown > 0 ? (drawdown / maxDrawdown) * 100 : 0;

  return (
    <div className="flex flex-col gap-6">
      {/* Circuit breaker status cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {circuitBreakers.map((cb) => (
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
          <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
            <div className="text-center">
              <div className="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-lg bg-bg-tertiary">
                <TrendingDown className="h-5 w-5 text-text-muted" />
              </div>
              <p className="text-sm font-medium text-text-muted">Drawdown Gauge</p>
              <p className="mt-1 text-xs text-text-light">{drawdown}% / {maxDrawdown}% max</p>
            </div>
          </div>
          <div className="mt-4 space-y-2">
            <div className="flex justify-between text-xs">
              <span className="text-text-muted">Current Drawdown</span>
              <span className="font-medium text-status-warning">{drawdown}%</span>
            </div>
            <div className="h-3 rounded-full bg-bg-tertiary overflow-hidden">
              <div
                className="h-full rounded-full bg-status-warning transition-all"
                style={{ width: `${drawdownPct}%` }}
              />
            </div>
            <div className="flex justify-between text-xs text-text-light">
              <span>0%</span>
              <span>{maxDrawdown}% (Kill switch)</span>
            </div>
          </div>
        </DataCard>

        {/* Daily loss tracker */}
        <DataCard title="Daily Loss Tracker" description="Cumulative losses today">
          <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border-default bg-bg-secondary">
            <div className="text-center">
              <div className="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-lg bg-bg-tertiary">
                <Activity className="h-5 w-5 text-text-muted" />
              </div>
              <p className="text-sm font-medium text-text-muted">Daily Loss Chart</p>
              <p className="mt-1 text-xs text-text-light">
                {loading ? 'Loading...' : 'Hourly loss accumulation'}
              </p>
            </div>
          </div>
          <div className="mt-4 space-y-2">
            {[
              { label: 'Max daily loss', value: `-$${state.dailyLoss.toFixed(2)}`, color: 'text-status-error' },
              { label: 'Net daily PnL', value: '+$285.50', color: 'text-status-success' },
              { label: 'Losing trades today', value: '2 / 7', color: 'text-text-primary' },
              { label: 'Risk utilization', value: '38%', color: 'text-text-primary' },
            ].map((row) => (
              <div key={row.label} className="flex justify-between text-xs">
                <span className="text-text-muted">{row.label}</span>
                <span className={cn('font-medium', row.color)}>{row.value}</span>
              </div>
            ))}
          </div>
        </DataCard>
      </div>

      {/* Risk alerts */}
      <DataCard title="Recent Risk Events" description="Risk management actions taken today">
        <div className="space-y-3">
          {[
            { time: '14:32', message: 'Position stop-loss triggered on BTC/USDT short at $68,180', severity: 'warning' as const },
            { time: '11:15', message: 'Strategy cooldown activated for Breakout Scanner after 3 consecutive losses', severity: 'warning' as const },
            { time: '09:00', message: 'Daily risk limits reset. All circuit breakers cleared.', severity: 'info' as const },
          ].map((event, idx) => (
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
