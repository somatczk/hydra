'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { cn } from '@/components/ui/cn';
import { describeApiError, parseErrorDetail } from '@/lib/apiState';
import { fetchApi } from '@/lib/api';
import type { SessionResponse } from '@/lib/derive';
import { utc } from '@/lib/format';
import {
  LIVE_FACTS,
  PERPS_FACTS,
  liveStartOutcome,
  startOutcome,
  type ApproveResponse,
  type LiveApprovalChallenge,
  type LiveSessionStartRequest,
  type Outcome,
  type StartSessionRequest,
} from '@/lib/outcomes';
import { settle } from './useOutcomeSlot';

interface StrategyOption {
  id: string;
  name: string;
}

interface StrategyDetail extends StrategyOption {
  symbol: string;
  timeframes: { primary: string };
  /** P4: the venue the start's live leg answers to — a binanceusdm strategy gates on the perps ceremony. */
  exchange_id?: string;
}

const DECIMAL = /^\d+(\.\d{1,8})?$/;
const CODE_LENGTH = 6;
const CODE_RE = /^\d{6}$/;
const DECIDED = new Set(['approval_expired', 'already_decided', 'approval_failed']);

interface StartSessionDialogProps {
  /** Preselected from a Strategies row or a backtest result; otherwise the dialog asks. */
  strategyId?: string;
  onClose: () => void;
}

/** F3: start a paper session. Mount it only while open. UI-4 Q3: a Live option appears when the
 * gate is armed, and a live start is itself code-gated (R9's challenge, shown here, typed back). */
export function StartSessionDialog({ strategyId, onClose }: StartSessionDialogProps) {
  const router = useRouter();
  const { live, setOutcome } = useShellStatus();
  const [options, setOptions] = useState<StrategyOption[] | null>(null);
  const [picked, setPicked] = useState(strategyId ?? '');
  const [detail, setDetail] = useState<StrategyDetail | Error | null>(null);
  const [capital, setCapital] = useState('');
  const [failure, setFailure] = useState<Outcome | null>(null);
  const [mode, setMode] = useState<'paper' | 'live'>('paper');
  const [phase, setPhase] = useState<'form' | 'code'>('form');
  const [challenge, setChallenge] = useState<LiveApprovalChallenge | null>(null);
  const [code, setCode] = useState('');
  const [codeError, setCodeError] = useState<string | null>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const hadCodeError = useRef(false);

  useEffect(() => {
    if (strategyId) return;
    let alive = true;
    fetchApi<StrategyOption[]>('/api/strategies')
      .then((list) => alive && setOptions(list))
      .catch((e: unknown) => alive && setDetail(e instanceof Error ? e : new Error(String(e))));
    return () => {
      alive = false;
    };
  }, [strategyId]);

  useEffect(() => {
    if (!picked) return;
    let alive = true;
    setDetail(null);
    fetchApi<StrategyDetail>(`/api/strategies/${picked}`)
      .then((d) => alive && setDetail(d))
      .catch((e: unknown) => alive && setDetail(e instanceof Error ? e : new Error(String(e))));
    return () => {
      alive = false;
    };
  }, [picked]);

  useEffect(() => {
    if (phase === 'code') codeRef.current?.focus();
  }, [phase]);

  useEffect(() => {
    if (codeError && !hadCodeError.current) codeRef.current?.focus();
    hadCodeError.current = codeError !== null;
  }, [codeError]);

  // P4-E: a perp strategy's live leg answers to the separate perps gate, never the spot row.
  const known = detail !== null && !(detail instanceof Error) ? detail : null;
  const perp = known?.exchange_id === 'binanceusdm';
  const armed = perp ? live.data?.perps_armed === true : live.data?.live_armed === true;
  const livePicked = mode === 'live';
  const killSwitchOn = live.data?.kill_switch_state === 'active';
  const tier = live.data?.circuit_breaker_restrictions?.active_tier ?? 0;
  const capitalError = capital !== '' && !(DECIMAL.test(capital) && Number(capital) > 0) ? 'Enter an amount above 0 with up to 8 decimals.' : null;
  const locked = phase === 'code';

  const start = async () => {
    const req: StartSessionRequest = { strategy_id: picked, trading_mode: 'paper', ...(capital ? { paper_capital: capital } : {}) };
    const result = await settle(fetchApi<SessionResponse>('/api/trading/sessions', { method: 'POST', body: JSON.stringify(req) }));
    const outcome = startOutcome(req, result);
    if (outcome.navigate) {
      setOutcome(outcome);
      onClose();
      router.push(outcome.navigate);
    } else {
      setFailure(outcome);
    }
  };

  const liveReq = (): LiveSessionStartRequest => ({
    strategy_id: picked,
    ...(capital ? { paper_capital: capital } : {}),
    reason: `Live session: ${known?.name ?? picked}`,
  });

  const startLive = async () => {
    if (phase === 'code' && challenge) {
      const result = await settle(
        fetchApi<ApproveResponse>(`/api/activity/approvals/${challenge.approval_id}/approve`, {
          method: 'POST',
          body: JSON.stringify({ code }),
        }),
      );
      const outcome = liveStartOutcome(liveReq(), result, 'approve');
      if (outcome.navigate) {
        setOutcome(outcome);
        onClose();
        router.push(outcome.navigate);
        return;
      }
      if ('ok' in result && !result.ok.executed) {
        // The refusal consumed the code; a fresh start needs a fresh challenge.
        setFailure(outcome);
        setPhase('form');
        setChallenge(null);
        setCode('');
        setCodeError(null);
        return;
      }
      const d = 'err' in result ? parseErrorDetail(result.err) : null;
      if (d && DECIDED.has(d.code)) {
        const expired = d.code === 'approval_expired';
        setFailure({
          tone: 'caution',
          title: expired ? `The one-time code expired at ${utc(challenge.expires_at) ?? 'its expiry'}. Nothing changed.` : d.message,
          lines: [expired ? 'Start it again for a fresh code.' : 'Nothing was started from this screen.'],
          actions: [],
        });
        setPhase('form');
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
      return;
    }
    const result = await settle(
      fetchApi<LiveApprovalChallenge>('/api/live/session-start', { method: 'POST', body: JSON.stringify(liveReq()) }),
    );
    if ('ok' in result) {
      setPhase('code');
      setChallenge(result.ok);
      setCode('');
      setCodeError(null);
      setFailure(null);
      return;
    }
    setFailure(liveStartOutcome(liveReq(), { err: result.err }, 'challenge'));
  };

  return (
    <ConfirmDialog
      open
      destructive={livePicked}
      title={livePicked ? 'Start a live session' : 'Start a paper session'}
      primaryLabel={livePicked ? 'Start live session' : 'Start paper session'}
      secondaryLabel="Cancel"
      pendingLabel="Starting…"
      pendingHint="Closing this won't cancel the start."
      primaryDisabled={!picked || killSwitchOn || capitalError !== null || (locked && !CODE_RE.test(code))}
      onClose={onClose}
      onConfirm={livePicked ? startLive : start}
    >
      {!strategyId && (
        <Select
          label="Strategy"
          placeholder={options ? 'Choose a strategy' : 'Loading strategies…'}
          options={(options ?? []).map((o) => ({ value: o.id, label: o.name }))}
          value={picked}
          disabled={locked}
          onChange={(e) => setPicked(e.target.value)}
        />
      )}
      {picked && (
        <p>
          {known
            ? `${known.name} · ${perp ? 'Binance USD⌂-M perps' : known.exchange_id === 'bybit' ? 'Bybit spot (live only)' : 'Binance spot'} · ${known.symbol} · ${known.timeframes.primary} · ${livePicked ? 'LIVE' : 'PAPER'}`
            : detail instanceof Error
              ? `Strategy details couldn't be read: ${describeApiError(detail, 'unknown error')}`
              : 'Reading the strategy…'}
        </p>
      )}
      <div role="group" aria-label="Trading mode" className="flex rounded-lg border border-line bg-surface p-1 lg:inline-flex">
        {(['paper', 'live'] as const).map((m) => (
          <button
            key={m}
            type="button"
            aria-pressed={mode === m}
            disabled={locked || (m === 'live' && !armed)}
            onClick={() => setMode(m)}
            className={cn(
              'h-11 flex-1 rounded-md px-4 text-sm font-medium capitalize lg:h-8',
              mode === m ? 'bg-raised text-ink-1' : 'text-ink-2 hover:text-ink-1',
            )}
          >
            {m}
          </button>
        ))}
      </div>
      {!armed && (
        <p className="text-xs text-ink-2">
          {perp
            ? 'Live needs the perps gate on — turn on perps trading in Settings first. '
            : 'Live needs the gate on — turn on live trading in Settings first. '}
          <Link href="/settings" className="underline">
            Settings ›
          </Link>
        </p>
      )}
      <Input
        label={livePicked ? 'Live capital for this session (USDT)' : 'Paper capital for this session (USDT)'}
        inputMode="decimal"
        autoComplete="off"
        value={capital}
        disabled={locked}
        onChange={(e) => setCapital(e.target.value.trim())}
        error={capitalError ?? undefined}
        hint={
          locked
            ? undefined
            : livePicked
              ? perp
                ? 'Leave empty to use all unallocated perps capital.'
                : 'Leave empty to use all unallocated live capital.'
              : 'Leave empty to use all unallocated paper capital.'
        }
      />
      {livePicked && <p className="font-semibold text-ink-1">{perp ? PERPS_FACTS : LIVE_FACTS}</p>}
      {phase === 'code' && challenge && (
        <>
          <p className="text-sm text-ink-1">
            One-time code: <span className="font-mono text-base font-semibold tracking-widest">{challenge.code}</span> — enter
            it once to start the session.
          </p>
          <p className="text-xs text-ink-2">The code expires {utc(challenge.expires_at) ?? '—'}.</p>
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
      {killSwitchOn && (
        <p role="alert" className="text-danger">
          The kill switch is on. Release it on Risk before starting a session.{' '}
          <Link href="/risk#kill-switch" className="underline">
            Release on Risk ›
          </Link>
        </p>
      )}
      {live.data === null && live.state !== 'loading' && (
        <p className="text-caution">Kill switch state unknown. The server will refuse the start if it is on.</p>
      )}
      {tier >= 2 && (
        <p className="text-caution">{`Breaker tier ${tier} is active. The session will start but can't open entries until it clears.`}</p>
      )}
      {failure && (
        <div role="alert" className={failure.tone === 'danger' ? 'text-danger' : 'text-caution'}>
          <p className="font-semibold">{failure.title}</p>
          {failure.lines.map((l) => (
            <p key={l}>{l}</p>
          ))}
          {failure.actions.map((a) =>
            a.href ? (
              <Link key={a.label} href={a.href} className="underline">
                {a.label}
              </Link>
            ) : null,
          )}
        </div>
      )}
    </ConfirmDialog>
  );
}
