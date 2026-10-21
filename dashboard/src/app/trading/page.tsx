'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { StartSessionDialog } from '@/components/sessions/StartSessionDialog';
import { StopSessionDialog } from '@/components/sessions/StopSessionDialog';
import { useOutcomeSlot } from '@/components/sessions/useOutcomeSlot';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { Button } from '@/components/ui/Button';
import { cn } from '@/components/ui/cn';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { fetchApi } from '@/lib/api';
import { heldAfter, isOverdue, plural, sessionName, type SessionDetail, type SessionResponse } from '@/lib/derive';
import { signed, utc } from '@/lib/format';

const DETAIL_POLL_MS = 15_000;
/** E-4: above this many running sessions the list stops fetching per-session detail. */
const MAX_DETAIL_SESSIONS = 10;

type Details = Map<string, SessionDetail | Error>;

/** Per-running-session detail (positions, stop failures) every 15 s; empty above the cap. */
function useRunningDetails(ids: string[]) {
  const key = ids.length > MAX_DETAIL_SESSIONS ? '' : ids.join(',');
  const [details, setDetails] = useState<Details>(new Map());
  const load = useCallback(async () => {
    const wanted = key ? key.split(',') : [];
    const entries = await Promise.all(
      wanted.map(async (id): Promise<[string, SessionDetail | Error]> => {
        try {
          return [id, await fetchApi<SessionDetail>(`/api/trading/sessions/${id}/detail`)];
        } catch (e) {
          return [id, e instanceof Error ? e : new Error(String(e))];
        }
      }),
    );
    return new Map(entries);
  }, [key]);
  const reload = useCallback(() => void load().then(setDetails), [load]);
  useEffect(() => {
    let alive = true;
    const run = () => void load().then((m) => alive && setDetails(m));
    run();
    const timer = setInterval(run, DETAIL_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [load]);
  return { details, reload, capped: ids.length > MAX_DETAIL_SESSIONS };
}

interface Cell {
  text: string;
  caution?: boolean;
}

interface RowData {
  session: SessionResponse;
  market: string;
  since: string;
  health: Cell[];
  positions: string;
  holds: string | null;
}

function rowData(s: SessionResponse, d: SessionDetail | Error | undefined, capped: boolean, now: number): RowData {
  const detail = d && !(d instanceof Error) ? d : null;
  const lastBarAt = detail?.last_bar_at ?? s.last_bar_at ?? null;
  const paused = detail?.entries_paused ?? s.entries_paused ?? null;
  const overdue = isOverdue(lastBarAt, s.timeframe, now);
  const failures = detail?.protection_failures.length;
  const health: Cell[] = [
    { text: `Last bar ${overdue === null ? 'warming up' : overdue ? 'overdue' : 'on time'}`, caution: overdue === true },
    { text: paused ? `Entries ${paused}` : 'Entries allowed', caution: !!paused },
    capped
      ? { text: 'Open for health' }
      : { text: `Stop failures ${failures === undefined ? '—' : failures === 0 ? 'none' : failures}`, caution: !!failures },
  ];
  if (s.feed_live === false) health.push({ text: 'Feed not live', caution: true });
  if (s.persist_dirty) health.push({ text: 'Unsaved ledger changes', caution: true });
  if (s.persist_error) health.push({ text: `Not saved: ${s.persist_error}`, caution: true });
  const unrealised = signed(detail?.metrics.unrealized_pnl);
  return {
    session: s,
    market: `${s.symbols.join(', ')} · ${s.timeframe}`,
    since: s.status === 'running' ? `since ${utc(s.started_at, now) ?? '—'}` : `stopped ${utc(s.stopped_at, now) ?? '—'}`,
    health,
    positions: detail ? `${plural(detail.positions.length, 'position')}${unrealised ? ` · ${unrealised} USDT` : ''}` : '—',
    holds: s.status !== 'running' && (s.positions_at_stop > 0 || s.flatten_incomplete)
      ? s.positions_at_stop > 0 ? `still holds ${s.positions_at_stop}` : 'still holds positions'
      : null,
  };
}

const HeldChip = ({ text }: { text: string }) => (
  <span className="rounded bg-danger-soft px-2 py-0.5 text-xs font-medium text-danger">{text}</span>
);

function PhoneList({ rows }: { rows: RowData[] }) {
  return (
    <ul className="flex flex-col gap-2 lg:hidden">
      {rows.map((r) => (
        <li key={r.session.session_id}>
          <Link
            href={`/trading/${r.session.session_id}`}
            className="flex min-h-12 flex-col gap-1 rounded border border-line bg-surface p-4 hover:bg-hover"
          >
            <span className="flex items-center gap-2 font-semibold text-ink-1">
              {sessionName(r.session)}
              {r.holds && <HeldChip text={r.holds} />}
            </span>
            <span className="text-sm text-ink-2">
              {r.market} · {r.since}
            </span>
            <span className="text-sm">
              {r.health.map((c, i) => (
                <span key={c.text} className={c.caution ? 'text-caution' : 'text-ink-1'}>
                  {i > 0 && ' · '}
                  {c.text}
                </span>
              ))}
            </span>
            <span className="text-sm text-ink-1">{r.positions}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function DesktopTable({ rows, onStop }: { rows: RowData[]; onStop: (s: SessionResponse) => void }) {
  return (
    <div className="hidden overflow-x-auto lg:block">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs font-semibold uppercase tracking-wider text-ink-2">
            <th className="px-3 py-2">Session</th>
            <th className="px-3 py-2">Market</th>
            <th className="px-3 py-2">Health</th>
            <th className="px-3 py-2">Positions</th>
            <th className="px-3 py-2">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.session.session_id} className="border-b border-line">
              <td className="px-3 py-2">
                <Link href={`/trading/${r.session.session_id}`} className="font-semibold underline">
                  {sessionName(r.session)}
                </Link>{' '}
                {r.holds && <HeldChip text={r.holds} />}
              </td>
              <td className="px-3 py-2 text-ink-2">
                {r.market} · {r.since}
              </td>
              <td className="px-3 py-2">
                {r.health.map((c, i) => (
                  <span key={c.text} className={c.caution ? 'text-caution' : 'text-ink-1'}>
                    {i > 0 && ' · '}
                    {c.text}
                  </span>
                ))}
              </td>
              <td className="px-3 py-2">{r.positions}</td>
              <td className="px-3 py-2 text-right">
                <Button variant="secondary" size="sm" onClick={() => onStop(r.session)}>
                  {r.session.status === 'running' ? 'Stop session…' : 'Try closing again…'}
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SessionRows({ rows, onStop }: { rows: RowData[]; onStop: (s: SessionResponse) => void }) {
  return (
    <>
      <PhoneList rows={rows} />
      <DesktopTable rows={rows} onStop={onStop} />
    </>
  );
}

export default function SessionsPage() {
  const { sessions, now, refetchAll } = useShellStatus();
  const list = sessions.data;
  const running = (list ?? []).filter((s) => s.status === 'running');
  const holders = list ? heldAfter(list) : [];
  const quiet = (list ?? []).filter((s) => s.status !== 'running' && !holders.includes(s));
  const { details, reload, capped } = useRunningDetails(running.map((s) => s.session_id));
  const [starting, setStarting] = useState(false);
  const [target, setTarget] = useState<SessionResponse | null>(null);
  const [showStopped, setShowStopped] = useState(false);

  const slot = useOutcomeSlot(
    (id) => {
      const s = list?.find((x) => x.session_id === id);
      return s ? sessionName(s) : id;
    },
    () => {
      reload();
      void refetchAll();
    },
  );

  const rows = (ss: SessionResponse[]) => ss.map((s) => rowData(s, details.get(s.session_id), capped, now));
  const targetDetail = target ? details.get(target.session_id) : undefined;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-ink-1">Sessions</h1>
        {sessions.state === 'stale' && sessions.fetchedAt !== null && (
          <DataStateBadge state="stale" reason={`Sessions as of ${utc(sessions.fetchedAt, now)}`} />
        )}
        <Button size="lg" className="max-lg:w-full lg:ml-auto" onClick={() => setStarting(true)}>
          Start paper session
        </Button>
      </div>

      {slot.panel}

      {list === null ? (
        sessions.state === 'loading' ? (
          <div className="h-32 animate-pulse rounded border border-line bg-surface" aria-label="Loading sessions" />
        ) : (
          <DataStateErrorCard error={sessions.error} fallback="Failed to load sessions" onRetry={() => void sessions.refetch()} />
        )
      ) : (
        <>
          <section aria-labelledby="running-heading" className="flex flex-col gap-3">
            <h2 id="running-heading" className="text-base font-semibold text-ink-1">
              Running ({running.length})
            </h2>
            {running.length === 0 ? (
              <p className="text-sm text-ink-2">Nothing is running. Start a paper session from a strategy.</p>
            ) : (
              <SessionRows rows={rows(running)} onStop={setTarget} />
            )}
          </section>

          {holders.length > 0 && (
            <section aria-labelledby="holders-heading" className="flex flex-col gap-3">
              <h2 id="holders-heading" className="text-base font-semibold text-ink-1">
                Stopped, still holding ({holders.length})
              </h2>
              <SessionRows rows={rows(holders)} onStop={setTarget} />
            </section>
          )}

          {quiet.length > 0 && (
            <section aria-labelledby="stopped-heading" className="flex flex-col gap-3">
              <h2 id="stopped-heading" className="text-base font-semibold text-ink-1">
                <button
                  type="button"
                  aria-expanded={showStopped}
                  onClick={() => setShowStopped(!showStopped)}
                  className={cn('inline-flex h-11 items-center rounded px-1 hover:bg-hover')}
                >
                  Stopped ({quiet.length})
                </button>
              </h2>
              {showStopped && <SessionRows rows={rows(quiet)} onStop={setTarget} />}
            </section>
          )}
        </>
      )}

      {starting && <StartSessionDialog onClose={() => setStarting(false)} />}
      {target && (
        <StopSessionDialog
          session={target}
          positions={targetDetail && !(targetDetail instanceof Error) ? targetDetail.positions : null}
          open
          onClose={() => setTarget(null)}
          onStop={() => slot.stop(target.session_id)}
        />
      )}
    </div>
  );
}
