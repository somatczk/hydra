import { describeApiError, stateFromError, type DataState } from '@/lib/apiState';
import { ErrorCard } from './ErrorCard';
import { StatusBadge } from './StatusBadge';

export type BadgeState = Exclude<DataState, 'ok'> | 'mock';

type Variant = 'error' | 'warning' | 'neutral';

const BADGES: Record<BadgeState, { label: string; variant: Variant }> = {
  unavailable: { label: 'UNAVAILABLE', variant: 'error' },
  stale: { label: 'STALE', variant: 'warning' },
  unknown: { label: 'UNKNOWN', variant: 'warning' },
  no_data: { label: 'NO DATA', variant: 'neutral' },
  not_found: { label: 'NOT FOUND', variant: 'neutral' },
  invalid: { label: 'INVALID', variant: 'error' },
  not_implemented: { label: 'NOT IMPLEMENTED', variant: 'neutral' },
  mock: { label: 'MOCK', variant: 'warning' },
};

interface DataStateBadgeProps {
  state: DataState | 'mock';
  /** Shown as a tooltip, e.g. "not wired to engine yet" or "last update 12:00:05". */
  reason?: string;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
}

/** Marks data that is not live and trustworthy; renders nothing for 'ok'. */
export function DataStateBadge({ state, reason, size = 'sm', className }: DataStateBadgeProps) {
  if (state === 'ok') return null;
  const badge = BADGES[state];
  return (
    <span role="status" data-state={state} title={reason} className="inline-flex">
      <StatusBadge status={badge.label} variant={badge.variant} size={size} className={className} />
    </span>
  );
}

interface DataStateErrorCardProps {
  error: unknown;
  /** Used when the server gave no message, e.g. "Failed to load positions". */
  fallback: string;
  onRetry?: () => void;
}

/** ErrorCard with the server's message and the matching data-state badge. */
export function DataStateErrorCard({ error, fallback, onRetry }: DataStateErrorCardProps) {
  return (
    <div className="flex flex-col gap-2">
      <DataStateBadge state={stateFromError(error)} />
      <ErrorCard message={describeApiError(error, fallback)} onRetry={onRetry} />
    </div>
  );
}
