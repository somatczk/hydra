'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Section } from '@/components/ui/Section';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { settle } from '@/components/sessions/useOutcomeSlot';
import { fetchApi } from '@/lib/api';
import { describeApiError, parseErrorDetail } from '@/lib/apiState';
import { plural, sessionName, type SessionResponse } from '@/lib/derive';
import { utc } from '@/lib/format';
import { LIVE_FACTS, liveGateOutcome, type ApproveResponse, type DisarmResponse, type LiveApprovalChallenge, type Outcome } from '@/lib/outcomes';

const CODE_LENGTH = 6;
const CODE_RE = /^\d{6}$/;
const DECIMAL = /^\d+(\.\d{1,8})?$/;
const DECIDED = new Set(['approval_expired', 'already_decided', 'approval_failed']);

/** A live session R10's disarm answer confirmed, bridged until the next sessions read lands. */
interface DisarmBridge {
  ids: string[];
  at: number;
}

function Failure({ outcome }: { outcome: Outcome }) {
  return (
    <div role="alert" className={outcome.tone === 'danger' ? 'text-danger' : 'text-caution'}>
      <p className="font-semibold">{outcome.title}</p>
      {outcome.lines.map((l) => (
        <p key={l}>{l}</p>
      ))}
      {outcome.actions.map((a) =>
        a.href ? (
          <Link key={a.label} href={a.href} className="underline">
            {a.label}
          </Link>
        ) : null,
      )}
    </div>
  );
}

/** The live sessions that keep running with their per-session Stop link; never hidden in any gate state. */
function RunningLive({ rows, sentence }: { rows: Array<{ id: string; name: string }>; sentence: string }) {
  return (
    <div className="rounded border border-live bg-live-soft p-3">
      <p className="text-sm font-semibold text-live">{sentence}</p>
      <ul className="mt-1 flex flex-col gap-1 text-sm">
        {rows.map((r) => (
          <li key={r.id} className="flex items-center justify-between gap-2">
            <Link href={`/trading/${r.id}`} className="font-medium underline">
              {r.name}
            </Link>
            <Link href={`/trading/${r.id}`} className="underline">
              Stop
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The 08 §6 Settings gate: arming is L4 (capital → the engine-issued code shown once, typed back),
 * disarm is the ungated L2 toward safety — and no state ever hides a running live session (R10). */
export function LiveGate() {
  const { live, sessions, refetchAll } = useShellStatus();
  const [armOpen, setArmOpen] = useState(false);
  const [phase, setPhase] = useState<'facts' | 'code'>('facts');
  const [capital, setCapital] = useState('');
  const [challenge, setChallenge] = useState<LiveApprovalChallenge | null>(null);
  const [code, setCode] = useState('');
  const [codeError, setCodeError] = useState<string | null>(null);
  const [failure, setFailure] = useState<Outcome | null>(null);
  const [serviceDown, setServiceDown] = useState(false);
  const [disarmOpen, setDisarmOpen] = useState(false);
  const [disarmError, setDisarmError] = useState<string | null>(null);
  const [bridge, setBridge] = useState<DisarmBridge | null>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const hadCodeError = useRef(false);

  const loading = live.state === 'loading';
  const armed = live.data?.live_armed === true;
  const unreadable = live.data === null && !loading;

  // R10's disarm ids bridge the gap until the next sessions read replaces them.
  useEffect(() => {
    if (bridge && sessions.fetchedAt !== null && sessions.fetchedAt > bridge.at) setBridge(null);
  }, [sessions.fetchedAt, bridge]);

  const shellRunning = (sessions.data ?? []).filter(isRunningLive);
  const runningRows = [...new Set([...(bridge?.ids ?? []), ...shellRunning.map((s) => s.session_id)])].map((id) => {
    const s = (sessions.data ?? []).find((x) => x.session_id === id);
    return { id, name: s ? sessionName(s) : id.slice(0, 8) };
  });
  const runningSentence = `${plural(runningRows.length, 'live session')} ${
    runningRows.length === 1 ? 'keeps' : 'keep'
  } running under ${runningRows.length === 1 ? 'its' : 'their'} start's authorization — bounded by the kill switch, risk limits and exchange stops.`;

  useEffect(() => {
    if (phase === 'code') codeRef.current?.focus();
  }, [phase]);

  useEffect(() => {
    if (codeError && !hadCodeError.current) codeRef.current?.focus();
    hadCodeError.current = codeError !== null;
  }, [codeError]);

  const capitalError =
    capital !== '' && !(DECIMAL.test(capital) && Number(capital) > 0) ? 'Enter an amount above 0 with up to 8 decimals.' : null;

  const openArm = () => {
    setPhase('facts');
    setChallenge(null);
    setCode('');
    setCodeError(null);
    setFailure(null);
    setArmOpen(true);
  };

  const arm = async () => {
    const result = await settle(
      fetchApi<LiveApprovalChallenge>('/api/live/arm', {
        method: 'POST',
        body: JSON.stringify({ live_capital: Number(capital), reason: 'Live trading armed from Settings' }),
      }),
    );
    if ('ok' in result) {
      setPhase('code');
      setChallenge(result.ok);
      setCode('');
      setCodeError(null);
      setFailure(null);
      return;
    }
    setFailure(liveGateOutcome({ err: result.err }, 'challenge'));
    const d = parseErrorDetail(result.err);
    if (d === null || d.state === 'unavailable') setServiceDown(true);
  };

  const approve = async () => {
    if (!challenge) return;
    const result = await settle(
      fetchApi<ApproveResponse>(`/api/activity/approvals/${challenge.approval_id}/approve`, {
        method: 'POST',
        body: JSON.stringify({ code }),
      }),
    );
    const outcome = liveGateOutcome(result, 'approve');
    if ('ok' in result && result.ok.executed) {
      setArmOpen(false);
      setPhase('facts');
      setChallenge(null);
      setCode('');
      setFailure(null);
      void refetchAll();
      return;
    }
    const d = 'err' in result ? parseErrorDetail(result.err) : null;
    if (d && DECIDED.has(d.code)) {
      const expired = d.code === 'approval_expired';
      setFailure({
        tone: 'caution',
        title: expired ? `The one-time code expired at ${utc(challenge.expires_at) ?? 'its expiry'}. Nothing changed.` : d.message,
        lines: [expired ? 'Turn it on again for a fresh code.' : 'Nothing changed from this screen.'],
        actions: [],
      });
      setPhase('facts');
      setChallenge(null);
      setCode('');
      setCodeError(null);
      return;
    }
    if (outcome.inline) {
      setCodeError([outcome.title, ...outcome.lines].join(' '));
      return;
    }
    setFailure(outcome);
  };

  const disarm = async () => {
    const result = await settle(
      fetchApi<DisarmResponse>('/api/live/disarm', {
        method: 'POST',
        body: JSON.stringify({ reason: 'Live trading turned off from Settings' }),
      }),
    );
    if ('ok' in result) {
      setBridge({ ids: result.ok.running_live_sessions, at: Date.now() });
      setDisarmError(null);
      setDisarmOpen(false);
      void refetchAll();
      return;
    }
    setDisarmError(describeApiError(result.err, "Live trading wasn't turned off. Try again."));
  };

  return (
    <>
    <Section title="Live trading">
      <div className="flex flex-col gap-3">
        {runningRows.length > 0 && <RunningLive rows={runningRows} sentence={runningSentence} />}
        {loading ? (
          <div className="h-16 animate-pulse rounded bg-raised" aria-label="Loading the live gate" />
        ) : unreadable ? (
          <p className="text-sm text-caution">Live gate state unknown.</p>
        ) : armed ? (
          <>
            <p className="text-sm text-ink-1">
              Live trading is on. Each live session start and manual live order still needs its own one-time code.
            </p>
            <div>
              <Button variant="outline" size="sm" onClick={() => setDisarmOpen(true)}>
                Turn off live trading…
              </Button>
            </div>
          </>
        ) : serviceDown ? (
          <div className="flex flex-col gap-1">
            <p className="text-sm font-semibold text-caution">{"Live trading wasn't turned on."}</p>
            <p className="text-sm text-ink-2">Live actions are hidden until the code service recovers.</p>
            <div>
              <Button variant="secondary" size="sm" onClick={() => setServiceDown(false)}>
                Try again
              </Button>
            </div>
          </div>
        ) : (
          <>
            <p className="text-sm text-ink-1">Paper. Every live order will need a one-time code.</p>
            <div>
              <Button variant="outline" size="sm" onClick={openArm}>
                Turn on live trading…
              </Button>
            </div>
          </>
        )}
      </div>
      </Section>

      <ConfirmDialog
        open={armOpen}
        destructive
        title="Turn on live trading?"
        primaryLabel="Turn on live trading"
        secondaryLabel="Cancel"
        pendingLabel="Turning on…"
        pendingHint="Closing this won't undo a turn-on already sent."
        primaryDisabled={phase === 'code' ? !CODE_RE.test(code) : capital === '' || capitalError !== null}
        onClose={() => setArmOpen(false)}
        onConfirm={phase === 'code' ? approve : arm}
      >
        <p className="font-semibold text-ink-1">{LIVE_FACTS}</p>
        {phase === 'facts' ? (
          <Input
            label="Live capital (USDT)"
            inputMode="decimal"
            autoComplete="off"
            value={capital}
            onChange={(e) => setCapital(e.target.value.trim())}
            error={capitalError ?? undefined}
            hint="The USDT live sessions allocate from; set with this switch, changed by turning live on again."
          />
        ) : (
          <>
            <p className="text-sm text-ink-1">
              One-time code: <span className="font-mono text-base font-semibold tracking-widest">{challenge?.code}</span> —
              enter it once to turn on live trading.
            </p>
            <p className="text-xs text-ink-2">The code expires {utc(challenge?.expires_at) ?? '—'}.</p>
            <Input
              ref={codeRef}
              label="One-time code"
              value={code}
              onChange={(e) => {
                setCode(e.target.value.replace(/\D/g, '').slice(0, CODE_LENGTH));
                setCodeError(null);
              }}
              autoComplete="one-time-code"
              inputMode="numeric"
              pattern="[0-9]*"
              maxLength={CODE_LENGTH}
              error={codeError ?? undefined}
              className="font-mono tracking-widest"
            />
          </>
        )}
        {failure && <Failure outcome={failure} />}
      </ConfirmDialog>

      <ConfirmDialog
        open={disarmOpen}
        title="Turn off live trading?"
        primaryLabel="Turn off live trading"
        secondaryLabel="Keep it on"
        pendingLabel="Turning off…"
        pendingHint="Closing this won't undo the turn-off."
        onClose={() => {
          setDisarmOpen(false);
          setDisarmError(null);
        }}
        onConfirm={disarm}
      >
        <p>{"Turning live trading off stops new live sessions from starting. It doesn't stop sessions already running."}</p>
        {runningRows.length > 0 ? (
          <>
            <p>{runningSentence}</p>
            <ul className="flex flex-col gap-1">
              {runningRows.map((r) => (
                <li key={r.id} className="flex items-center justify-between gap-2">
                  <Link href={`/trading/${r.id}`} className="underline">
                    {r.name}
                  </Link>
                  <Link href={`/trading/${r.id}`} className="underline">
                    Stop
                  </Link>
                </li>
              ))}
            </ul>
          </>
        ) : sessions.data === null ? (
          <p className="text-caution">The sessions list is unreadable — live sessions may be running. Check /trading before disarming.</p>
        ) : (
          <p>No live session is running.</p>
        )}
        {disarmError && (
          <p role="alert" className="text-danger">
            {disarmError}
          </p>
        )}
      </ConfirmDialog>
    </>
  );
}

const isRunningLive = (s: SessionResponse): boolean => s.trading_mode === 'live' && s.status === 'running';
