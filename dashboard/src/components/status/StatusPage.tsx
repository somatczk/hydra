'use client';

import { useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react';
import Link from 'next/link';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, XAxis, YAxis } from 'recharts';
import { Button } from '@/components/ui/Button';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { Pnl } from '@/components/ui/Pnl';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { usePoll } from '@/hooks/usePoll';
import { fetchApi } from '@/lib/api';
import { fetchAgeLabel } from '@/lib/apiState';
import { age, money, utc } from '@/lib/format';
import { accounts, isOverdue, modeBadge, plural, sessionName, type Account as Acct, type SessionDetail, type SessionResponse } from '@/lib/derive';
import { AttentionList } from './AttentionList';
import { Readout } from './Readout';
import { Section } from './Section';
import {
  dataView,
  needsAnswer,
  recentEvents,
  type Answer,
  runningAnswer,
  safeAnswer,
  type ReadinessLite,
  type RiskStatusLite,
} from './answers';

const DETAIL_POLL_MS = 15_000;
const DESKTOP = '(min-width: 1024px)';
// jsdom has no matchMedia; treat it as desktop.
const subscribeDesktop = (cb: () => void) => {
  const mq = window.matchMedia?.(DESKTOP);
  mq?.addEventListener('change', cb);
  return () => mq?.removeEventListener('change', cb);
};
const useDesktop = () => useSyncExternalStore(subscribeDesktop, () => window.matchMedia?.(DESKTOP).matches ?? true, () => true);
// shortcut: detail polling is capped at 10 running sessions, raise it when /sessions carries last_bar_at.
const DETAIL_CAP = 10;

interface Summary {
  total_value: number;
  unrealized_pnl: number;
  realized_pnl: number;
  daily_pnl: number;
  total_fees: number;
  paper_capital_pending: number | null;
}

interface EquityPoint {
  timestamp: string;
  value: number;
}

type Details = Map<string, SessionDetail | Error>;

/** Details of the running sessions, refetched every 15 s; `ready` once the first round has settled. */
function useRunningDetails(ids: string[] | null): { details: Details; ready: boolean } {
  const [state, setState] = useState<{ key: string; details: Details } | null>(null);
  const key = ids ? ids.slice(0, DETAIL_CAP).join(',') : null;

  useEffect(() => {
    if (key === null) return;
    const wanted = key === '' ? [] : key.split(',');
    const ctrl = new AbortController();
    const load = async () => {
      const got = await Promise.all(
        wanted.map(async (id): Promise<[string, SessionDetail | Error]> => {
          try {
            return [id, await fetchApi<SessionDetail>(`/api/trading/sessions/${id}/detail`, { signal: ctrl.signal })];
          } catch (e) {
            return [id, e instanceof Error ? e : new Error(String(e))];
          }
        }),
      );
      if (!ctrl.signal.aborted) setState({ key, details: new Map(got) });
    };
    void load();
    const timer = setInterval(() => void load(), DETAIL_POLL_MS);
    return () => {
      ctrl.abort();
      clearInterval(timer);
    };
  }, [key]);

  return { details: state?.details ?? new Map(), ready: state !== null && state.key === key };
}

function AgeLine({ fetchedAt, state, now, onRetry }: { fetchedAt: number | null; state: Parameters<typeof fetchAgeLabel>[1]; now: number; onRetry?: () => void }) {
  const label = fetchAgeLabel(fetchedAt, state, now);
  if (label === null) return null;
  if (typeof label === 'string') return <span className="text-xs text-ink-2">{label}</span>;
  return (
    <span className="flex items-center gap-2 text-xs text-ink-2">
      <DataStateBadge state="stale" reason={`as of ${label.asOf}`} />
      as of {label.asOf}
      {onRetry && (
        <Button variant="ghost" size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </span>
  );
}

function MoneyLine({ source, label }: { source: 'paper' | 'live'; label: ReactNode }) {
  const { now } = useShellStatus();
  const summary = usePoll<Summary>(`/api/portfolio/summary?source=${source}`, 15_000);
  const s = summary.data;
  if (!s && summary.state !== 'loading') {
    return <DataStateErrorCard error={summary.error} fallback="Account couldn't be loaded" onRetry={() => void summary.refetch()} />;
  }
  if (!s) return <p className="text-sm text-ink-2">Loading…</p>;
  if (s.total_value <= 0) {
    return (
      <p className="flex items-center gap-2 text-sm text-ink-1">
        <DataStateBadge state="unknown" />
        Account value couldn&apos;t be determined
      </p>
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {label}{' '}
        <span className="font-display text-3xl font-medium text-ink-1">{money(s.total_value)}</span>{' '}
        <span className="text-sm text-ink-2">USDT</span>
        <AgeLine fetchedAt={summary.fetchedAt} state={summary.state} now={now} onRetry={() => void summary.refetch()} />
      </p>
      <dl className="grid max-w-md grid-cols-2 gap-x-4 text-sm">
        <dt className="border-t border-line py-1 text-ink-2">Realised today</dt>
        <dd className="border-t border-line py-1 text-right"><Pnl value={s.daily_pnl} unit="USDT" /></dd>
        <dt className="border-t border-line py-1 text-ink-2">Unrealised</dt>
        <dd className="border-t border-line py-1 text-right"><Pnl value={s.unrealized_pnl} unit="USDT" /></dd>
        <dt className="border-t border-line py-1 text-ink-2">Realised</dt>
        <dd className="border-t border-line py-1 text-right"><Pnl value={s.realized_pnl} unit="USDT" /></dd>
        <dt className="border-y border-line py-1 text-ink-2">Fees</dt>
        <dd className="border-y border-line py-1 text-right text-ink-1">{money(s.total_fees)} USDT</dd>
      </dl>
      <p className="text-xs text-ink-2">Unrealised: mark time not reported.</p>
      {s.paper_capital_pending !== null && (
        <p className="text-sm text-caution">
          Paper capital change to {money(s.paper_capital_pending)} USDT waits for a ledger reset
        </p>
      )}
    </div>
  );
}

const LiveStamp = () => (
  <span className="bg-live px-1.5 font-display text-sm font-semibold tracking-wider text-on-live">LIVE</span>
);

/** Paper and live money side by side, never summed; the live line exists only when something is live. */
function Money({ live }: { live: boolean }) {
  return (
    <section id="money" aria-labelledby="money-h" className="flex flex-col gap-3">
      <div className="flex items-baseline gap-3">
        <h2 id="money-h" className="text-xl font-semibold text-ink-1">Money</h2>
        <Link href="/portfolio" className="text-sm text-accent">Performance</Link>
      </div>
      <div className="flex flex-wrap gap-x-12 gap-y-6">
        {live && <MoneyLine source="live" label={<LiveStamp />} />}
        <MoneyLine source="paper" label={<span className="font-medium text-ink-2">Paper</span>} />
      </div>
    </section>
  );
}

function AccountCaption({ a }: { a: Acct }) {
  return (
    <span className="flex flex-wrap items-center gap-2 text-sm">
      <span className="font-semibold text-ink-1">{a.venue}</span>{' '}
      {a.mode === 'live' ? <LiveStamp /> : <span className="text-ink-2">paper</span>}{' '}
      <span className="text-ink-2">· {plural(a.sessions.length, 'session')}</span>
    </span>
  );
}

function rowView(s: SessionResponse, details: Details, ready: boolean, now: number) {
  const d = details.get(s.session_id);
  const row = d && !(d instanceof Error) ? d : null;
  const overdue = row ? isOverdue(row.last_bar_at, s.timeframe, now) : null;
  const lastBar = row
    ? row.last_bar_at
      ? `${age(now - Date.parse(row.last_bar_at), now)} · ${overdue ? 'overdue' : 'on time'}`
      : 'No closed bar yet'
    : null;
  return { row, lastBar, failed: !row && (ready || d !== undefined) };
}

const Unreadable = () => (
  <span className="flex items-center gap-2"><DataStateBadge state="unknown" />Couldn&apos;t read this session</span>
);

function AccountTable({ a, details, ready, now }: { a: Acct; details: Details; ready: boolean; now: number }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[680px] text-left text-sm">
        <caption className="pb-2 text-left"><AccountCaption a={a} /></caption>
        <thead className="text-xs text-ink-2">
          <tr className="border-b border-line-strong">
            {['Name', 'Market', 'Last bar', 'Entries', 'Stop failures', 'Positions', 'Unrealised (USDT)'].map((h) => (
              <th key={h} scope="col" title={h.startsWith('Unrealised') ? 'Marked at the last closed bar' : undefined} className={h.startsWith('Unrealised') ? 'whitespace-nowrap py-2 text-right font-medium' : 'whitespace-nowrap py-2 pr-3 font-medium'}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {a.sessions.map((s) => {
            const { row, lastBar, failed } = rowView(s, details, ready, now);
            return (
              <tr key={s.session_id} className={row?.protection_failures.length ? 'border-b border-line bg-caution-soft' : 'border-b border-line'}>
                <td className="py-2.5 pr-3">
                  <Link href={`/trading/${s.session_id}`} className="text-accent">{sessionName(s)}</Link>
                </td>
                <td className="whitespace-nowrap pr-3 text-ink-2">{s.symbols.join(', ')} · {s.timeframe}</td>
                {row ? (
                  <>
                    <td className="whitespace-nowrap pr-3 text-ink-2">{lastBar}</td>
                    <td className="whitespace-nowrap pr-3">{row.entries_paused ?? 'allowed'}</td>
                    <td className={row.protection_failures.length ? 'pr-3 font-semibold text-danger' : 'pr-3'}>{row.protection_failures.length}</td>
                    <td className="whitespace-nowrap pr-3">{plural(row.positions.length, 'position')}</td>
                    <td className="text-right font-medium"><Pnl value={row.positions.reduce((n, p) => n + p.unrealized_pnl, 0)} /></td>
                  </>
                ) : (
                  <td colSpan={5}>{failed ? <Unreadable /> : 'Loading…'}</td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function AccountList({ a, details, ready, now }: { a: Acct; details: Details; ready: boolean; now: number }) {
  return (
    <div className="flex flex-col">
      <h3 className="border-b border-line-strong pb-1.5"><AccountCaption a={a} /></h3>
      <ul>
        {a.sessions.map((s) => {
          const { row, lastBar, failed } = rowView(s, details, ready, now);
          const stopFailed = (row?.protection_failures.length ?? 0) > 0;
          return (
            <li key={s.session_id} className="border-b border-line">
              <Link href={`/trading/${s.session_id}`} className="flex min-h-14 items-center justify-between gap-3 py-1.5">
                <span className="flex min-w-0 flex-col">
                  <span className="truncate font-medium text-ink-1">{sessionName(s)}</span>
                  <span className={stopFailed ? 'text-xs font-semibold text-danger' : 'text-xs text-ink-2'}>
                    {row
                      ? `${s.symbols.join(', ')} · ${plural(row.positions.length, 'position')} · ${stopFailed ? 'stop failed' : lastBar}`
                      : failed ? <Unreadable /> : 'Loading…'}
                  </span>
                </span>
                {row && <Pnl className="shrink-0 font-medium" value={row.positions.reduce((n, p) => n + p.unrealized_pnl, 0)} />}
              </Link>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function RunningNow({ details, ready, desktop }: { details: Details; ready: boolean; desktop: boolean }) {
  const { sessions, now } = useShellStatus();
  const groups = sessions.data ? accounts(sessions.data) : [];
  return (
    <section id="running-now" aria-labelledby="running-now-h" className="flex min-w-0 flex-col gap-5 lg:col-span-8">
      <h2 id="running-now-h" className="text-xl font-semibold text-ink-1">Running now</h2>
      {groups.length === 0 ? (
        <p className="text-sm text-ink-2">
          {sessions.data
            ? 'Nothing is running. Start a paper session from a strategy.'
            : sessions.state === 'loading'
              ? 'Loading…'
              : "Couldn't read sessions."}
        </p>
      ) : (
        groups.map((a) =>
          desktop ? (
            <AccountTable key={a.key} a={a} details={details} ready={ready} now={now} />
          ) : (
            <AccountList key={a.key} a={a} details={details} ready={ready} now={now} />
          ),
        )
      )}
      {groups.length > 0 && <p className="text-xs text-ink-2">Unrealised is marked at the last closed bar.</p>}
    </section>
  );
}

function Equity() {
  const curve = usePoll<EquityPoint[]>('/api/portfolio/equity-curve?source=paper', 60_000);
  const pts = curve.data;
  const summary = useMemo(() => {
    if (!pts || pts.length === 0) return null;
    const values = pts.map((p) => p.value);
    return `Equity from ${money(values[0])} to ${money(values[values.length - 1])} USDT, peak ${money(Math.max(...values))} USDT.`;
  }, [pts]);
  return (
    <Section title="Paper equity">
      {!pts && curve.state !== 'loading' ? (
        <DataStateErrorCard error={curve.error} fallback="Equity history couldn't be loaded" onRetry={() => void curve.refetch()} />
      ) : !pts ? (
        <p className="text-sm text-ink-2">Loading…</p>
      ) : !summary ? (
        <p className="flex items-center gap-2 text-sm text-ink-1">
          <DataStateBadge state="no_data" />
          No equity history yet.
        </p>
      ) : (
        <div role="img" aria-label={summary} className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={pts} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="var(--line)" vertical={false} />
              <XAxis dataKey="timestamp" tickFormatter={(t: string) => utc(t) ?? ''} tick={{ fontSize: 12, fill: 'var(--ink-2)' }} minTickGap={48} />
              <YAxis domain={['auto', 'auto']} tick={{ fontSize: 12, fill: 'var(--ink-2)' }} width={64} label={{ value: 'USDT', angle: -90, position: 'insideLeft', fill: 'var(--ink-2)', fontSize: 12 }} />
              <Line type="monotone" dataKey="value" stroke="var(--ink-1)" strokeWidth={2} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </Section>
  );
}

export function StatusPage() {
  const shell = useShellStatus();
  const desktop = useDesktop();
  const { live, sessions, config, approvals, needs, now } = shell;
  const risk = usePoll<RiskStatusLite>('/api/risk/status', 10_000);
  const readiness = usePoll<ReadinessLite>('/api/system/readiness', 15_000);
  // shortcut: counts at most the newest 500 events, replace with a server-side since= filter when one exists.
  const events = usePoll<Array<{ ts: string | null }>>('/api/risk/events?limit=500', 30_000);
  const runningIds = sessions.data ? sessions.data.filter((s) => s.status === 'running').map((s) => s.session_id) : null;
  const { details, ready } = useRunningDetails(runningIds);

  const run = runningAnswer(sessions);
  const safe = safeAnswer(live, risk, now);
  const data = dataView(sessions, details, ready, now, readiness);
  const need = needsAnswer(needs, [live, sessions, config, approvals].some((p) => p.state === 'loading'));
  const hist = recentEvents(events.data, now);

  const headPart = (a: Answer, fallback: string) => (
    <>
      {a.state !== 'ok' && <DataStateBadge state={a.state} />}
      {a.value ?? fallback}
    </>
  );

  return (
    <div className="flex flex-col gap-10">
      <div className="flex flex-col gap-4">
        <h1 className="font-display text-4xl font-semibold leading-tight text-ink-1 lg:text-5xl">
          <Link href="/trading" className="hover:underline">{headPart(run, 'Checking sessions')}</Link>.{' '}
          <Link href="#needs-you" className={need.tone === 'none' ? 'hover:underline' : 'underline decoration-caution decoration-[3px] underline-offset-4'}>
            {headPart({ ...need, value: need.value === '?' ? 'Needs you: ?' : need.value }, 'Checking what needs you')}
          </Link>
          .
        </h1>
        <div className="grid grid-cols-1 lg:grid-cols-2 lg:gap-x-8">
          <Readout label="Safe" {...safe} href="/risk" />
          <Readout label="Data" {...data} href="/trading" />
        </div>
      </div>
      <div className="grid grid-cols-1 gap-10 lg:grid-cols-12 lg:gap-x-12">
        <RunningNow details={details} ready={ready} desktop={desktop} />
        <section id="needs-you" aria-labelledby="needs-you-h" className="flex min-w-0 flex-col gap-4 lg:col-span-4">
          <h2 id="needs-you-h" className="text-xl font-semibold text-ink-1">Needs you</h2>
          <AttentionList
            needs={needs}
            loading={[live, sessions, config, approvals].some((p) => p.state === 'loading')}
            history={
              hist === null ? (
                events.state === 'loading' ? 'Checking risk events…' : 'Risk events couldn’t be read.'
              ) : hist === 0 ? (
                'No risk events since yesterday.'
              ) : (
                <>
                  Since yesterday: {plural(hist, 'risk event')} ·{' '}
                  <Link href="/activity" className="text-accent">Activity log</Link>
                </>
              )
            }
          />
        </section>
      </div>
      <Money live={modeBadge(live.data, sessions.data) === 'live'} />
      {desktop && <Equity />}
    </div>
  );
}
