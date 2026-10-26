import { Button } from '@/components/ui/Button';
import { cn } from '@/components/ui/cn';
import { breakerRow, type CircuitBreaker } from '@/lib/derive';
import { pct } from '@/lib/format';

/** circuit_breakers.py AUTO_RESET_HOURS: tiers 3 and 4 never reset on their own. */
const AUTO_RESET: Record<number, string> = { 1: '24 h', 2: '48 h' };

interface BreakerTableProps {
  breakers: CircuitBreaker[];
  activeTier: number;
  now: number;
  onReset: (tier: number) => void;
}

/** Four tiers, one row each; only the active tier offers a reset. */
export function BreakerTable({ breakers, activeTier, now, onReset }: BreakerTableProps) {
  return (
    <ul className="divide-y divide-line text-sm">
      {breakers.map((b) => {
        const row = breakerRow(b, activeTier, now) ?? { text: 'Unknown', tone: 'caution' as const };
        const active = b.tier === activeTier;
        return (
          <li key={b.tier} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-3">
            <div className="min-w-0">
              <p className="font-medium text-ink-1">
                Tier {b.tier}: {b.label}
              </p>
              <p className={cn('text-ink-2', row.tone === 'caution' && 'text-caution', row.tone === 'danger' && 'text-danger')}>
                {row.text}
              </p>
              <p className="text-xs text-ink-2">Drawdown now {pct(b.current_value)}</p>
              {active && AUTO_RESET[b.tier] && (
                <p className="text-xs text-ink-2">
                  Resets automatically after {AUTO_RESET[b.tier]} once drawdown is below {pct(b.threshold_pct)}.
                </p>
              )}
            </div>
            {active && (
              <Button variant="secondary" size="sm" aria-haspopup="dialog" onClick={() => onReset(b.tier)}>
                Reset tier {b.tier}…
              </Button>
            )}
          </li>
        );
      })}
    </ul>
  );
}
