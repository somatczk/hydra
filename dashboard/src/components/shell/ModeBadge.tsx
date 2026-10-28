import type { TradingMode } from '@/lib/derive';

/** C-MODE: PAPER outlined, LIVE filled with the live tokens plus 'real money' in text (08 §6),
 * and '?' when the mode is unreadable (R23 — the badge never says PAPER over real money). */
export function ModeBadge({ mode }: { mode: TradingMode | null }) {
  if (mode === 'live') {
    return (
      <span className="inline-flex flex-wrap items-center gap-2">
        <span className="inline-flex h-6 items-center rounded bg-live px-2 text-xs font-semibold tracking-wide text-on-live">
          <span aria-hidden="true">LIVE</span>
          <span className="sr-only">Live trading: real money</span>
        </span>
        <span aria-hidden="true" className="text-xs font-semibold text-live">
          real money
        </span>
      </span>
    );
  }
  if (mode === null) {
    return (
      <span className="inline-flex h-6 items-center rounded border border-line-strong px-2 text-xs font-semibold tracking-wide text-ink-2">
        <span aria-hidden="true">?</span>
        <span className="sr-only">Trading mode unknown</span>
      </span>
    );
  }
  return (
    <span className="inline-flex h-6 items-center rounded border border-ink-2 px-2 text-xs font-semibold tracking-wide text-ink-2">
      <span aria-hidden="true">PAPER</span>
      <span className="sr-only">Paper trading: no real money</span>
    </span>
  );
}
