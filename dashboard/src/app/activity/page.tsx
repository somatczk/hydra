'use client';

import { useState } from 'react';
import Link from 'next/link';
import { ActivityLog, type RiskEvent, type RowLink } from '@/components/activity/ActivityLog';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { cn } from '@/components/ui/cn';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { Section } from '@/components/ui/Section';
import { usePoll } from '@/hooks/usePoll';
import { actionPhrase } from '@/lib/derive';
import { utc } from '@/lib/format';

const TRAIL_PATH = '/api/activity?limit=500';

type Filter = 'all' | 'claude' | 'me' | 'services';

const FILTERS: Array<{ id: Filter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'claude', label: 'Claude' },
  { id: 'me', label: 'Me' },
  { id: 'services', label: 'Services' },
];

/** service:mcp=Claude, user:*=Me, everything else a service (06 §6 actor grouping). */
const actorGroup = (actor: string): Filter =>
  actor === 'service:mcp' ? 'claude' : actor.startsWith('user:') ? 'me' : 'services';

const EMPTY_BY_FILTER: Record<Filter, string> = {
  all: 'No risk events yet. Breaker changes, resets and kill-switch changes appear here.',
  claude: 'No Claude activity yet.',
  me: 'No activity by you yet.',
  services: 'No service activity yet.',
};

/** The touched object when the event's details name one: a session row, a strategy. */
function rowLink(e: RiskEvent): RowLink | null {
  if (typeof e.details.session_id === 'string' && e.details.session_id) {
    return { href: `/trading/${e.details.session_id}`, label: 'Open session' };
  }
  if (typeof e.details.strategy_id === 'string' && e.details.strategy_id) {
    return { href: `/builder?strategy=${e.details.strategy_id}`, label: 'Open strategy' };
  }
  return null;
}

export default function ActivityPage() {
  const { approvals, now } = useShellStatus();
  const trail = usePoll<RiskEvent[]>(TRAIL_PATH, 30_000);
  const [filter, setFilter] = useState<Filter>('all');

  const events = trail.data === null ? null : filter === 'all' ? trail.data : trail.data.filter((e) => actorGroup(e.actor) === filter);

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold text-ink-1">Activity</h1>

      <Section title="Waiting for you">
        {approvals.state === 'loading' && approvals.data === null ? (
          <p className="text-sm text-ink-2">Checking…</p>
        ) : approvals.data === null ? (
          <p className="flex flex-wrap items-center gap-2 text-sm text-ink-1">
            <DataStateBadge state="unknown" />
            Approvals: unknown
          </p>
        ) : approvals.data.length === 0 ? (
          <p className="text-sm text-ink-2">No approvals waiting.</p>
        ) : (
          <ul className="flex flex-col divide-y divide-line rounded-lg border border-line">
            {approvals.data.map((a) => (
              <li key={a.id}>
                <Link
                  href={`/activity/approvals/${a.id}`}
                  className="flex min-h-12 items-center px-3 py-2 text-sm text-ink-1 hover:bg-hover"
                >
                  Claude asks to {actionPhrase(a.action)} · requested {utc(a.requested_at, now) ?? 'time not reported'} · Review
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section
        title="Trail"
        action={
          <div role="group" aria-label="Filter by who" className="flex flex-wrap gap-2">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                aria-pressed={filter === f.id}
                onClick={() => setFilter(f.id)}
                className={cn(
                  'h-8 max-lg:h-11 rounded border px-3 text-sm',
                  filter === f.id ? 'border-accent font-medium text-ink-1' : 'border-line text-ink-2 hover:bg-hover hover:text-ink-1',
                )}
              >
                {f.label}
              </button>
            ))}
          </div>
        }
      >
        {trail.data === null ? (
          trail.state === 'loading' ? (
            <p className="text-sm text-ink-2">Loading…</p>
          ) : (
            <DataStateErrorCard error={trail.error} fallback="Activity could not be loaded" onRetry={() => void trail.refetch()} />
          )
        ) : events !== null && events.length > 0 ? (
          <ActivityLog events={events} now={now} rowLink={rowLink} />
        ) : (
          <p className="text-sm text-ink-2">{EMPTY_BY_FILTER[filter]}</p>
        )}
        {trail.state === 'stale' && trail.fetchedAt !== null && (
          <p className="mt-2 flex items-center gap-2 text-xs text-ink-2">
            <DataStateBadge state="stale" reason={`as of ${utc(trail.fetchedAt, now)}`} />
          </p>
        )}
      </Section>
    </div>
  );
}
