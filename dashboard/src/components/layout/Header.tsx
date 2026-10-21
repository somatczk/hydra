'use client';

import type { ReactNode } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Moon, Sun } from 'lucide-react';
import { useTheme } from './ThemeProvider';
import { ConditionBar, breakerPhrase } from './ConditionBar';
import { activeItem } from './navItems';
import { KillSwitchControl } from '@/components/shell/KillSwitchControl';
import { ModeBadge } from '@/components/shell/ModeBadge';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { DataStateBadge } from '@/components/ui/DataStateBadge';
import { cn } from '@/components/ui/cn';
import { fetchAgeLabel, type PollState } from '@/lib/apiState';
import { plural } from '@/lib/derive';
import { utc } from '@/lib/format';

const Skeleton = () => <span aria-hidden="true" className="inline-block h-4 w-20 animate-pulse rounded bg-hover" />;

/** One strip cell: an `<a>` to where you act on it, plus STALE when its source missed two polls. */
function Cell({ href, tone, stale, children }: { href?: string; tone?: string; stale?: string | null; children: ReactNode }) {
  const body = href ? (
    <Link href={href} className={cn('underline-offset-2 hover:underline', tone)}>
      {children}
    </Link>
  ) : (
    <span className={tone}>{children}</span>
  );
  return (
    <span className="inline-flex items-center gap-2 border-l border-line pl-4 text-sm tabular-nums max-lg:hidden">
      {body}
      {stale && <DataStateBadge state="stale" reason={`as of ${stale}`} />}
    </span>
  );
}

/** ≥1024 px: the instrument strip. Below: app bar (title, PAPER, kill switch) + condition bar. Pages own their h1. */
export function Header() {
  const pathname = usePathname();
  const { resolved, setTheme } = useTheme();
  const { live, sessions, config, needs, now } = useShellStatus();

  const staleAt = (p: { state: PollState; fetchedAt: number | null }) =>
    p.state === 'stale' && p.fetchedAt !== null ? utc(p.fetchedAt, now) : null;
  const liveStale = staleAt(live);
  const loading = (p: { state: PollState }) => p.state === 'loading';

  const r = live.data?.circuit_breaker_restrictions;
  const breaker = loading(live) ? (
    <Cell><Skeleton /></Cell>
  ) : !live.data || live.data.circuit_breakers_state === 'unavailable' || !r ? (
    <Cell href="/risk#breakers" tone="text-caution">Breakers UNAVAILABLE</Cell>
  ) : (
    <Cell
      href="/risk#breakers"
      stale={liveStale}
      tone={r.active_tier >= 3 ? 'text-danger font-semibold' : r.active_tier >= 1 ? 'text-caution' : undefined}
    >
      Tier {r.active_tier} · {breakerPhrase(r)}
    </Cell>
  );

  const running = live.data?.running_sessions;
  const sessionsCell = loading(live) ? (
    <Cell><Skeleton /></Cell>
  ) : running === null || running === undefined ? (
    <Cell href="/trading" tone="text-caution">Sessions UNKNOWN</Cell>
  ) : (
    <Cell href="/trading" stale={liveStale}>
      {running === 0 ? 'Nothing running' : `${plural(running, 'session')} running`}
    </Cell>
  );

  const polls = [live, sessions, config];
  const fetched = polls.map((p) => p.fetchedAt).filter((t): t is number => t !== null);
  const age = polls.some(loading)
    ? fetchAgeLabel(null, 'loading', now)
    : fetched.length === 0
      ? null
      : fetchAgeLabel(Math.min(...fetched), polls.some((p) => p.state === 'stale') ? 'stale' : 'ok', now);
  const dataCell = (
    <Cell tone={typeof age === 'string' ? 'text-ink-2' : 'text-caution'}>
      {typeof age === 'string' ? age : age ? `Data STALE · as of ${age.asOf}` : 'Data UNAVAILABLE'}
    </Cell>
  );

  const needsCell = polls.some(loading) ? (
    <Cell><Skeleton /></Cell>
  ) : (
    <Cell
      href="/#needs-you"
      stale={polls.some((p) => p.state === 'stale') ? utc(Math.min(...fetched), now) : null}
      tone={needs.count ? 'font-semibold' : undefined}
    >
      {needs.count === null ? 'Needs you: ?' : needs.count === 0 ? 'Nothing needs you' : `Needs you: ${needs.count}`}
    </Cell>
  );

  return (
    <header className="sticky top-0 z-30 border-b border-line bg-canvas lg:pl-56">
      <div className="flex min-h-14 items-center gap-2 px-4 py-1.5 lg:flex-wrap lg:gap-x-4 lg:px-6">
        <span className="min-w-0 flex-1 truncate text-base font-semibold lg:hidden">{activeItem(pathname)?.label ?? 'Hydra'}</span>
        <ModeBadge storedMode={config.data?.trading_mode} />
        <KillSwitchControl />
        {breaker}
        {sessionsCell}
        {dataCell}
        {needsCell}
        <button
          type="button"
          onClick={() => setTheme(resolved === 'dark' ? 'light' : 'dark')}
          className="ml-auto inline-flex h-8 w-8 items-center justify-center rounded text-ink-2 hover:bg-hover hover:text-ink-1 max-lg:hidden"
          aria-label={`Switch to ${resolved === 'dark' ? 'light' : 'dark'} mode`}
        >
          {resolved === 'dark' ? <Sun className="h-5 w-5" aria-hidden="true" /> : <Moon className="h-5 w-5" aria-hidden="true" />}
        </button>
      </div>
      <ConditionBar />
    </header>
  );
}
