'use client';

import Link from 'next/link';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { heldAfter, plural, type Restrictions } from '@/lib/derive';
import { utc } from '@/lib/format';

interface Condition {
  text: string;
  href?: string;
}

export function breakerPhrase(r: Restrictions): string {
  if (r.is_shutdown) return 'shut down';
  if (r.must_flatten) return 'flattening';
  return r.can_open_new_trades ? 'entries allowed' : 'entries halted';
}

/** Phone only: one line of C-COND, shown only when something is abnormal (the strip says it all at ≥1024 px). */
export function ConditionBar() {
  const { live, sessions, config, needs, ksNotSaved, now } = useShellStatus();
  const conditions: Condition[] = [];

  if (live.data?.kill_switch_state === 'active') {
    const holders = heldAfter(sessions.data ?? []);
    const positions = holders.reduce((n, s) => n + s.positions_at_stop, 0);
    const held = holders.length ? ` · ${positions > 0 ? plural(positions, 'position') : 'positions'} still held` : '';
    conditions.push({ text: `Kill switch ON${ksNotSaved ? ' · not saved' : ''}${held}`, href: '/risk#kill-switch' });
  }
  const r = live.data?.circuit_breaker_restrictions;
  if (r && r.active_tier >= 1) {
    conditions.push({ text: `Breaker tier ${r.active_tier} · ${breakerPhrase(r)}`, href: '/risk#breakers' });
  }
  if (needs.count) {
    conditions.push({ text: `${plural(needs.count, 'thing')} ${needs.count === 1 ? 'needs' : 'need'} you`, href: '/#needs-you' });
  }
  const polls = [live, sessions, config];
  const stale = polls.filter((p) => p.state === 'stale' && p.fetchedAt !== null).map((p) => p.fetchedAt as number);
  if (stale.length) conditions.push({ text: `Data STALE · as of ${utc(Math.min(...stale), now)}` });
  const failed = (['risk status', 'sessions', 'settings'] as const).filter(
    (_, i) => !['loading', 'refreshing', 'ok', 'stale'].includes(polls[i].state),
  );
  if (failed.length) conditions.push({ text: `Couldn't read ${failed.join(', ')}` });

  return (
    <div role="status" aria-label="Conditions" className="lg:hidden">
      {conditions.length > 0 && (
        <p className="flex flex-wrap items-center gap-x-2 border-t border-line bg-caution-soft px-4 text-sm text-ink-1">
          {conditions.map((c, i) => (
            <span key={c.text} className="inline-flex min-h-11 items-center gap-2">
              {i > 0 && <span aria-hidden="true">·</span>}
              {c.href ? (
                <Link href={c.href} className="font-medium underline-offset-2 hover:underline">
                  {c.text} ›
                </Link>
              ) : (
                c.text
              )}
            </span>
          ))}
        </p>
      )}
    </div>
  );
}
