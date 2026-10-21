/** C-MODE: always PAPER in P1, because live orders are refused by the engine (case 11). */
export function ModeBadge({ storedMode }: { storedMode: string | null | undefined }) {
  const other = storedMode && storedMode !== 'paper' ? storedMode : null;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <span className="inline-flex h-6 items-center rounded border border-ink-2 px-2 text-xs font-semibold tracking-wide text-ink-2">
        <span aria-hidden="true">PAPER</span>
        <span className="sr-only">Paper trading: no real money</span>
      </span>
      {other && (
        <span className="text-xs text-caution max-lg:hidden">
          Stored mode is {other}; live trading is refused until a later release. Showing paper.
        </span>
      )}
    </span>
  );
}
