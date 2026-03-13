import { Zap, TrendingUp, Signal, Clock } from 'lucide-react';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { cn } from '@/components/ui/cn';

/* ---------- Mock data ---------- */

interface Strategy {
  id: string;
  name: string;
  description: string;
  status: 'Active' | 'Paused' | 'Backtesting' | 'Draft';
  statusVariant: 'success' | 'warning' | 'info' | 'neutral';
  pnl: string;
  pnlPositive: boolean;
  signals: number;
  trades: number;
  winRate: string;
  lastSignal: string;
}

const strategies: Strategy[] = [
  {
    id: '1',
    name: 'LSTM Momentum',
    description: 'ML-based momentum strategy using LSTM predictions on 1h BTC/USDT',
    status: 'Active',
    statusVariant: 'success',
    pnl: '+$1,240.50',
    pnlPositive: true,
    signals: 47,
    trades: 32,
    winRate: '68.7%',
    lastSignal: '12 min ago',
  },
  {
    id: '2',
    name: 'Mean Reversion RSI',
    description: 'RSI-based mean reversion with Bollinger Band confirmation',
    status: 'Active',
    statusVariant: 'success',
    pnl: '+$580.20',
    pnlPositive: true,
    signals: 23,
    trades: 18,
    winRate: '61.1%',
    lastSignal: '45 min ago',
  },
  {
    id: '3',
    name: 'Breakout Scanner',
    description: 'Volume-weighted breakout detection across multiple timeframes',
    status: 'Paused',
    statusVariant: 'warning',
    pnl: '-$120.00',
    pnlPositive: false,
    signals: 8,
    trades: 5,
    winRate: '40.0%',
    lastSignal: '3 hours ago',
  },
  {
    id: '4',
    name: 'XGBoost Ensemble',
    description: 'Ensemble of XGBoost models with feature importance weighting',
    status: 'Backtesting',
    statusVariant: 'info',
    pnl: 'N/A',
    pnlPositive: true,
    signals: 0,
    trades: 0,
    winRate: 'N/A',
    lastSignal: 'Never',
  },
];

/* ---------- Page ---------- */

export default function StrategiesPage() {
  return (
    <div className="flex flex-col gap-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-text-muted">
            {strategies.filter((s) => s.status === 'Active').length} active strategies
          </p>
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
              <StatusBadge
                status={strategy.status}
                variant={strategy.statusVariant}
                size="sm"
              />
            </div>

            {/* Stats row */}
            <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div>
                <p className="text-xs text-text-muted">PnL</p>
                <p
                  className={cn(
                    'text-sm font-semibold',
                    strategy.pnlPositive
                      ? 'text-status-success'
                      : 'text-status-error',
                  )}
                >
                  {strategy.pnl}
                </p>
              </div>
              <div>
                <p className="text-xs text-text-muted">Win Rate</p>
                <p className="text-sm font-semibold text-text-primary">
                  {strategy.winRate}
                </p>
              </div>
              <div className="flex items-center gap-1">
                <Signal className="h-3 w-3 text-text-light" aria-hidden="true" />
                <div>
                  <p className="text-xs text-text-muted">Signals</p>
                  <p className="text-sm font-semibold text-text-primary">
                    {strategy.signals}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <Clock className="h-3 w-3 text-text-light" aria-hidden="true" />
                <div>
                  <p className="text-xs text-text-muted">Last Signal</p>
                  <p className="text-sm text-text-secondary">
                    {strategy.lastSignal}
                  </p>
                </div>
              </div>
            </div>

            {/* Toggle area */}
            <div className="mt-4 flex items-center justify-between border-t border-border-default pt-3">
              <span className="text-xs text-text-muted">
                {strategy.trades} trades executed
              </span>
              <div
                className={cn(
                  'relative h-6 w-11 cursor-pointer rounded-full transition-colors',
                  strategy.status === 'Active'
                    ? 'bg-status-success'
                    : 'bg-bg-active',
                )}
                role="switch"
                aria-checked={strategy.status === 'Active'}
                aria-label={`${strategy.status === 'Active' ? 'Disable' : 'Enable'} ${strategy.name}`}
                tabIndex={0}
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
