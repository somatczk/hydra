'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { OctagonAlert } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { DataStateBadge } from '@/components/ui/DataStateBadge';
import { fetchApi } from '@/lib/api';
import { parseErrorDetail } from '@/lib/apiState';
import { ksTargets, plural, sessionName, stoppedHolders, type SessionDetail, type SessionResponse, type Tone } from '@/lib/derive';
import { utc } from '@/lib/format';
import {
  killSwitchOutcome,
  saveAgainOutcome,
  stopOutcome,
  type KillSwitchResponse,
  type Outcome,
  type OutcomeAction,
  type Result,
} from '@/lib/outcomes';
import { ConfirmDialog } from './ConfirmDialog';
import { OutcomePanel } from './OutcomePanel';
import { useShellStatus } from './ShellStatusProvider';

const KS_PATH = '/api/trading/kill-switch';
const RANK: Record<Tone, number> = { none: 0, caution: 1, danger: 2 };

async function settle<T>(p: Promise<T>): Promise<Result<T>> {
  try {
    return { ok: await p };
  } catch (err) {
    return { err };
  }
}

const notPersisted = (r: Result<unknown>) => 'err' in r && parseErrorDetail(r.err)?.code === 'kill_switch_not_persisted';

/** The kill-switch mutations; their outcomes land in the shell outcome slot. */
function useKillSwitchActions() {
  const shell = useShellStatus();
  const { setOutcome, setKsNotSaved, refetchAll } = shell;

  const activate = async () => {
    const result = await settle(fetchApi<KillSwitchResponse>(KS_PATH, { method: 'POST' }));
    const fresh = await fetchApi<SessionResponse[]>('/api/trading/sessions').catch((e: unknown) =>
      e instanceof Error ? e : new Error(String(e)),
    );
    const on = 'ok' in result || notPersisted(result);
    if (on) setKsNotSaved(!('ok' in result));
    setOutcome(killSwitchOutcome(result, fresh), on);
    void refetchAll();
  };

  const saveAgain = async () => {
    const result = await settle(fetchApi<KillSwitchResponse>(KS_PATH, { method: 'POST' }));
    const on = 'ok' in result || notPersisted(result);
    if (on) setKsNotSaved(!('ok' in result));
    setOutcome(saveAgainOutcome(result), on);
    void refetchAll();
  };

  /** C-KS-2b/3 "Try closing again": DELETE per holder, never the kill switch again. */
  const tryClosing = async (ids: string[]) => {
    const outs = await Promise.all(
      ids.map(async (id) =>
        stopOutcome(await settle(fetchApi<SessionResponse>(`/api/trading/sessions/${id}`, { method: 'DELETE' }))),
      ),
    );
    const again = (o: Outcome, id: string): OutcomeAction[] =>
      o.actions.map((a) => (a.kind === 'retry' ? { kind: 'try_closing', label: 'Try closing again', sessionIds: [id] } : a));
    if (outs.length === 1) {
      setOutcome({ ...outs[0], actions: again(outs[0], ids[0]) });
    } else {
      const name = (id: string) => {
        const s = shell.sessions.data?.find((x) => x.session_id === id);
        return s ? sessionName(s) : id;
      };
      const retryIds = ids.filter((id, i) => again(outs[i], id).some((a) => a.kind === 'try_closing'));
      setOutcome({
        tone: outs.reduce<Tone>((t, o) => (RANK[o.tone] > RANK[t] ? o.tone : t), 'none'),
        title: `Tried closing ${plural(ids.length, 'session')} again.`,
        lines: outs.map((o, i) => `${name(ids[i])}: ${[o.title, ...o.lines].join(' ')}`),
        actions: retryIds.length ? [{ kind: 'try_closing', label: 'Try closing again', sessionIds: retryIds }] : [],
      });
    }
    void refetchAll();
  };

  const run = (action: OutcomeAction): Promise<void> => {
    if (action.kind === 'save_again') return saveAgain();
    if (action.kind === 'try_closing') return tryClosing(action.sessionIds ?? []);
    return activate();
  };

  return { activate, run };
}

/** The shell's outcome slot: always mounted, so the injected outcome is announced. */
export function KillSwitchOutcome() {
  const { outcome, setOutcome } = useShellStatus();
  const { run } = useKillSwitchActions();
  const [busy, setBusy] = useState<OutcomeAction['kind'] | null>(null);
  const onAction = async (a: OutcomeAction) => {
    if (busy) return;
    setBusy(a.kind);
    try {
      await run(a);
    } finally {
      setBusy(null);
    }
  };
  return (
    <OutcomePanel
      outcome={outcome?.outcome ?? null}
      busy={busy}
      onAction={(a) => void onAction(a)}
      onDismiss={() => setOutcome(null)}
    />
  );
}

type Count = number | 'counting' | 'unknown';

/** C-KS-1 body: counts targets only and names stopped holders separately (R-P0-1). */
function SheetFacts({ sessions, count }: { sessions: SessionResponse[] | null; count: Count }) {
  if (!sessions) {
    return <p>This stops every running session and closes whatever they hold, at market (paper). Open positions: unknown.</p>;
  }
  const { running, errored } = ksTargets(sessions);
  const holders = stoppedHolders(sessions);
  const stops = <b>{plural(running.length, 'running session')}</b>;
  return (
    <>
      {typeof count === 'number' ? (
        <p>
          This stops {stops} and closes <b>{plural(count, 'open position')}</b> at market (paper). New sessions can&apos;t
          start until you release the kill switch.
        </p>
      ) : (
        <p>
          This stops {stops} and closes their open positions at market (paper). Open positions:{' '}
          {count === 'counting' ? 'counting…' : 'unknown'}
        </p>
      )}
      {errored.length > 0 && (
        <p>
          {errored.length === 1
            ? '1 errored session is also stopped and closed if it still runs or holds.'
            : `${errored.length} errored sessions are also stopped and closed if they still run or hold.`}
        </p>
      )}
      {holders.length > 0 && (
        <p>
          Still held in {plural(holders.length, 'stopped session')}: {holders.map(sessionName).join(', ')}. The kill
          switch won&apos;t close these; stop them individually.
        </p>
      )}
    </>
  );
}

const DETAIL_TIMEOUT_MS = 3_000;

/** C-KS-OFF/ON/UNK: the one kill-switch control, reading only /api/risk/live-status. Release lives on Risk. */
export function KillSwitchControl() {
  const { live, sessions, now } = useShellStatus();
  const { activate } = useKillSwitchActions();
  const [open, setOpen] = useState(false);
  const [count, setCount] = useState<Count>('unknown');
  const opened = useRef(0);
  const chip = useRef<HTMLAnchorElement>(null);
  const refocus = useRef(false);

  const openSheet = () => {
    const id = ++opened.current;
    setOpen(true);
    const running = sessions.data ? ksTargets(sessions.data).running : null;
    if (!running) return setCount('unknown');
    if (running.length === 0) return setCount(0);
    setCount('counting');
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), DETAIL_TIMEOUT_MS);
    void Promise.all(
      running.map((s) => fetchApi<SessionDetail>(`/api/trading/sessions/${s.session_id}/detail`, { signal: ctrl.signal })),
    )
      .then((details) => details.reduce((n, d) => n + d.positions.length, 0))
      .catch((): Count => 'unknown')
      .then((n) => {
        if (opened.current === id) setCount(n);
      })
      .finally(() => clearTimeout(timer));
  };

  const state = live.data?.kill_switch_state ?? (live.state === 'loading' ? 'loading' : 'unknown');
  // After Stop all the button is replaced by the chip; keep keyboard focus on the control.
  useEffect(() => {
    if (state !== 'active' || !refocus.current) return;
    refocus.current = false;
    if (document.activeElement === document.body || !document.activeElement) chip.current?.focus();
  }, [state]);
  const stale = live.state === 'stale' && live.fetchedAt !== null && (
    <DataStateBadge state="stale" reason={`Kill switch as of ${utc(live.fetchedAt, now)}`} />
  );

  if (state === 'active') {
    return (
      <span className="inline-flex items-center gap-2">
        <Link
          ref={chip}
          href="/risk#kill-switch"
          className="inline-flex h-8 items-center gap-1.5 rounded border border-danger bg-danger-soft px-3 text-sm font-semibold text-danger max-lg:h-11"
        >
          <OctagonAlert className="h-4 w-4" aria-hidden="true" />
          Kill switch ON<span className="font-normal max-lg:hidden"> · Release on Risk</span>
        </Link>
        {stale}
      </span>
    );
  }

  const label =
    state === 'unknown' ? (
      <>
        <DataStateBadge state="unknown" reason="Kill switch state unknown" />
        <span className="text-sm max-lg:hidden">Kill switch state unknown</span>
      </>
    ) : state === 'loading' ? (
      <span aria-hidden="true" className="h-4 w-24 animate-pulse rounded bg-hover max-lg:hidden" />
    ) : (
      <span className="text-sm max-lg:hidden">Kill switch off</span>
    );
  const primary = typeof count === 'number' && count > 0 ? `Stop all trading and close ${plural(count, 'position')}` : 'Stop all trading';

  return (
    <div className="inline-flex items-center gap-2">
      {label}
      <Button variant="secondary" size="sm" aria-label="Stop all trading" aria-haspopup="dialog" onClick={openSheet}>
        <OctagonAlert className="h-4 w-4" aria-hidden="true" />
        Stop all{state === 'unknown' ? '' : <span className="max-lg:hidden"> trading</span>}
      </Button>
      {stale}
      <ConfirmDialog
        open={open}
        destructive
        title="Stop all trading?"
        primaryLabel={primary}
        secondaryLabel="Cancel"
        pendingLabel="Stopping…"
        pendingHint="Closing this won't cancel the stop."
        onClose={() => setOpen(false)}
        onConfirm={async () => {
          refocus.current = true;
          await activate();
          setOpen(false);
        }}
      >
        <SheetFacts sessions={sessions.data} count={count} />
      </ConfirmDialog>
    </div>
  );
}
