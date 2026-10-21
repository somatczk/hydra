'use client';

import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import Link from 'next/link';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, XAxis, YAxis } from 'recharts';
import { Button } from '@/components/ui/Button';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { usePoll } from '@/hooks/usePoll';
import { fetchApi } from '@/lib/api';
import { fetchAgeLabel } from '@/lib/apiState';
import { age, money, signed, utc } from '@/lib/format';
import { isOverdue, plural, sessionName, type SessionDetail } from '@/lib/derive';
import { AttentionList } from './AttentionList';
import { Readout } from './Readout';
import { Section } from './Section';
import {
  dataView,
  needsAnswer,
  recentEvents,
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

function Account() {
  const { now } = useShellStatus();
  const summary = usePoll<Summary>('/api/portfolio/summary?source=paper', 15_000);
  const s = summary.data;
  return (
    <Section
      title="Paper account"
      action={<Link href="/portfolio" className="text-sm text-accent hover:underline">Performance ›</Link>}
    >
      {!s && summary.state !== 'loading' ? (
        <DataStateErrorCard error={summary.error} fallback="Account couldn't be loaded" onRetry={() => void summary.refetch()} />
      ) : !s ? (
        <p className="text-sm text-ink-2">Loading…</p>
      ) : s.total_value <= 0 ? (
        <p className="flex items-center gap-2 text-sm text-ink-1">
          <DataStateBadge state="unknown" />
          Account value couldn&apos;t be determined
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          <AgeLine fetchedAt={summary.fetchedAt} state={summary.state} now={now} onRetry={() => void summary.refetch()} />
          <p className="flex items-baseline gap-2">
            <span className="text-2xl font-semibold text-ink-1">{money(s.total_value)}</span>
            <span className="text-sm text-ink-2">USDT</span>
          </p>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
            <dt className="text-ink-2">Realised today</dt>
            <dd className="text-right text-ink-1">{signed(s.daily_pnl)} USDT</dd>
            <dt className="text-ink-2">Unrealised</dt>
            <dd className="text-right text-ink-1">{signed(s.unrealized_pnl)} USDT</dd>
            <dt className="text-ink-2">Realised</dt>
            <dd className="text-right text-ink-1">{signed(s.realized_pnl)} USDT</dd>
            <dt className="text-ink-2">Fees</dt>
            <dd className="text-right text-ink-1">{money(s.total_fees)} USDT</dd>
          </dl>
          <p className="text-xs text-ink-2">Unrealised: mark time not reported.</p>
          {s.paper_capital_pending !== null && (
            <p className="text-sm text-caution">
              Paper capital change to {money(s.paper_capital_pending)} USDT waits for a ledger reset
            </p>
          )}
        </div>
      )}
    </Section>
  );
}

function SessionsTable({ details, ready }: { details: Details; ready: boolean }) {
  const { sessions, now } = useShellStatus();
  const running = sessions.data?.filter((x) => x.status === 'running') ?? [];
  return (
    <Section title="Running sessions" className="lg:col-span-8">
      {running.length === 0 ? (
        <p className="text-sm text-ink-2">
          {sessions.data ? 'Nothing is running. Start a paper session from a strategy.' : "Couldn't read sessions."}
        </p>
      ) : (
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-ink-2">
            <tr>
              {['Name', 'Market', 'Started', 'Last bar', 'Entries', 'Stop failures', 'Positions', 'Unrealised (USDT)'].map((h) => (
                <th key={h} scope="col" title={h.startsWith('Unrealised') ? 'Marked at the last closed bar' : undefined} className="pb-2 pr-3 font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {running.map((s) => {
              const d = details.get(s.session_id);
              const row = d && !(d instanceof Error) ? d : null;
              const overdue = row ? isOverdue(row.last_bar_at, s.timeframe, now) : null;
              return (
                <tr key={s.session_id} className="border-t border-line">
                  <td className="py-2 pr-3">
                    <Link href={`/trading/${s.session_id}`} className="text-accent hover:underline">{sessionName(s)}</Link>
                  </td>
                  <td className="pr-3">{s.symbols.join(', ')} · {s.timeframe}</td>
                  <td className="pr-3">{utc(s.started_at, now) ?? '—'}</td>
                  {row ? (
                    <>
                      <td className="pr-3">
                        {row.last_bar_at
                          ? `${age(now - Date.parse(row.last_bar_at), now)} · ${overdue ? 'overdue' : 'on time'}`
                          : 'No closed bar yet'}
                      </td>
                      <td className="pr-3">{row.entries_paused ?? 'allowed'}</td>
                      <td className="pr-3">{row.protection_failures.length}</td>
                      <td className="pr-3">{plural(row.positions.length, 'position')}</td>
                      <td>{signed(row.positions.reduce((n, p) => n + p.unrealized_pnl, 0))}</td>
                    </>
                  ) : (
                    <td colSpan={5}>
                      {ready || d ? (
                        <span className="flex items-center gap-2"><DataStateBadge state="unknown" />Couldn&apos;t read this session</span>
                      ) : (
                        'Loading…'
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {running.length > 0 && <p className="mt-2 text-xs text-ink-2">Unrealised is marked at the last closed bar.</p>}
    </Section>
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
    <Section title="Equity" className="lg:col-span-12">
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
  const { live, sessions, config, needs, now } = shell;
  const risk = usePoll<RiskStatusLite>('/api/risk/status', 10_000);
  const readiness = usePoll<ReadinessLite>('/api/system/readiness', 15_000);
  // shortcut: counts at most the newest 500 events, replace with a server-side since= filter when one exists.
  const events = usePoll<Array<{ ts: string | null }>>('/api/risk/events?limit=500', 30_000);
  const runningIds = sessions.data ? sessions.data.filter((s) => s.status === 'running').map((s) => s.session_id) : null;
  const { details, ready } = useRunningDetails(runningIds);

  const run = runningAnswer(sessions);
  const safe = safeAnswer(live, risk, now);
  const data = dataView(sessions, details, ready, now, readiness);
  const need = needsAnswer(needs, [live, sessions, config].some((p) => p.state === 'loading'));
  const hist = recentEvents(events.data, now);

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold text-ink-1">Status</h1>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Readout label="Running" {...run} state={run.state} href="/trading" />
        <Readout label="Safe" {...safe} href="/risk" />
        <Readout label="Data" {...data} href="/trading" />
        <Readout label="Needs you" {...need} href="#needs-you" />
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        {desktop && <SessionsTable details={details} ready={ready} />}
        <div className="flex flex-col gap-4 lg:col-span-4">
          <Section title="Needs you" id="needs-you">
            <AttentionList
              needs={needs}
              loading={[live, sessions, config].some((p) => p.state === 'loading')}
              history={
                hist === null ? (
                  'Risk events couldn’t be read.'
                ) : hist === 0 ? (
                  'No risk events since yesterday.'
                ) : (
                  <>
                    Since yesterday: {plural(hist, 'risk event')} ·{' '}
                    <Link href="/risk#activity" className="text-accent hover:underline">Activity log ›</Link>
                  </>
                )
              }
            />
          </Section>
          <Account />
        </div>
        {desktop && <Equity />}
      </div>
    </div>
  );
}
