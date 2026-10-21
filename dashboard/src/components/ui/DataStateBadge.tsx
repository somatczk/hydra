import { useId } from 'react';
import { describeApiError, stateFromError, type DataState } from '@/lib/apiState';
import { ErrorCard } from './ErrorCard';
import { StatusBadge } from './StatusBadge';

export type BadgeState = Exclude<DataState, 'ok'>;

type Variant = 'error' | 'warning' | 'neutral';

const BADGES: Record<BadgeState, { label: string; variant: Variant }> = {
  unavailable: { label: 'UNAVAILABLE', variant: 'warning' },
  stale: { label: 'STALE', variant: 'warning' },
  unknown: { label: 'UNKNOWN', variant: 'warning' },
  no_data: { label: 'NO DATA', variant: 'neutral' },
  not_found: { label: 'NOT FOUND', variant: 'neutral' },
  invalid: { label: 'INVALID', variant: 'error' },
  not_implemented: { label: 'NOT IMPLEMENTED', variant: 'neutral' },
  unauthenticated: { label: 'NOT SIGNED IN', variant: 'warning' },
  forbidden: { label: 'FORBIDDEN', variant: 'warning' },
};

interface DataStateBadgeProps {
  state: DataState;
  /** Why, e.g. "as of 14:02:10 UTC"; tooltip plus screen-reader description. */
  reason?: string;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
}

/** Marks data that is not live and trustworthy; renders nothing for 'ok'. */
export function DataStateBadge({ state, reason, size = 'sm', className }: DataStateBadgeProps) {
  const reasonId = useId();
  if (state === 'ok') return null;
  const badge = BADGES[state];
  return (
    <>
      <span
        role="status"
        data-state={state}
        title={reason}
        aria-describedby={reason ? reasonId : undefined}
        className="inline-flex"
      >
        <StatusBadge status={badge.label} variant={badge.variant} size={size} className={className} />
      </span>
      {reason && (
        <span id={reasonId} className="sr-only">
          {reason}
        </span>
      )}
    </>
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
