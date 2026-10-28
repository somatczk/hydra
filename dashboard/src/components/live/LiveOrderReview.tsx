'use client';

import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/Button';
import { DataStateBadge } from '@/components/ui/DataStateBadge';
import { Input } from '@/components/ui/Input';
import { settle } from '@/components/sessions/useOutcomeSlot';
import { fetchApi } from '@/lib/api';
import { parseErrorDetail } from '@/lib/apiState';
import { sessionName, type SessionDetail } from '@/lib/derive';
import { utc } from '@/lib/format';
import {
  LIVE_FACTS,
  baseAsset,
  liveOrderOutcome,
  type ApproveResponse,
  type LiveApprovalChallenge,
  type Outcome,
  type QuickTradeRequest,
} from '@/lib/outcomes';

const CODE_LENGTH = 6;
const CODE_RE = /^\d{6}$/;

function Failure({ outcome }: { outcome: Outcome }) {
  return (
    <div role="alert" className={outcome.tone === 'danger' ? 'text-danger' : 'text-caution'}>
      <p className="font-semibold">{outcome.title}</p>
      {outcome.lines.map((l) => (
        <p key={l}>{l}</p>
      ))}
    </div>
  );
}

/** The 06 §10 order sentence: 'Buy 0.0015 BTC with real money on Binance · about 97.50 USDT · Stop at 64256 on the exchange'; sells reduce. */
function orderSentence(req: QuickTradeRequest, about: string): string {
  const base = baseAsset(req.symbol);
  const kind = req.order_type === 'LIMIT' ? ` (limit at ${req.price})` : '';
  if (req.side === 'sell') {
    return `Sell ${req.quantity} ${base} with real money on Binance${kind} · ${about} · This reduces the held position; no stop is needed.`;
  }
  const stop = req.stop_loss
    ? `Stop at ${req.stop_loss}${req.take_profit ? `, take-profit at ${req.take_profit}` : ''} on the exchange.`
    : req.trailing_stop_pct
      ? `Trailing stop ${req.trailing_stop_pct}% on the exchange.`
      : 'Strategy default stop on the exchange.';
  return `Buy ${req.quantity} ${base} with real money on Binance${kind} · ${about} · ${stop}`;
}

interface LiveOrderReviewProps {
  session: SessionDetail;
  /** The exact typed values from the form; the challenge is bound to them (R9's params). */
  req: QuickTradeRequest;
  /** The form's price context, e.g. "About 97.50 USDT at the last closed bar's price." */
  about: string;
  onOutcome: (outcome: Outcome) => void;
  /** After any request that reached the engine, so the page re-reads positions and fills. */
  onSent: () => void;
  /** A placed order clears the form's quantity. */
  onPlaced: () => void;
  onBack: () => void;
}

/** The L4 review step for one manual live order (08 §6): the R9 challenge is created for the exact
 * params, the engine-issued code is shown once here and typed back, and every failure that leaves
 * the order's existence unknown ends the challenge — this screen can never place a second order. */
export function LiveOrderReview({ session, req, about, onOutcome, onSent, onPlaced, onBack }: LiveOrderReviewProps) {
  const [challenge, setChallenge] = useState<LiveApprovalChallenge | null>(null);
  const [down, setDown] = useState(false);
  const [failure, setFailure] = useState<Outcome | null>(null);
  const [typed, setTyped] = useState('');
  const [code, setCode] = useState('');
  const [codeError, setCodeError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const codeRef = useRef<HTMLInputElement>(null);
  const hadCodeError = useRef(false);
  const sending = useRef(false);
  const sid = session.session_id;
  const reason = `Manual live order: ${req.side} ${req.quantity} ${baseAsset(req.symbol)} in ${sessionName(session)}`;
  const body = JSON.stringify({ ...req, reason });

  useEffect(() => {
    let alive = true;
    void settle(fetchApi<LiveApprovalChallenge>('/api/live/order', { method: 'POST', body })).then((result) => {
      if (!alive) return;
      if ('ok' in result) {
        setChallenge(result.ok);
        return;
      }
      const d = parseErrorDetail(result.err);
      if (d === null || d.state === 'unavailable') setDown(true);
      else setFailure(liveOrderOutcome(req, { err: result.err }, sid, 'challenge'));
    });
    return () => {
      alive = false;
    };
  }, [body, req, sid]);

  useEffect(() => {
    if (codeError && !hadCodeError.current) codeRef.current?.focus();
    hadCodeError.current = codeError !== null;
  }, [codeError]);

  const place = async () => {
    if (!challenge || sending.current || typed !== req.quantity || !CODE_RE.test(code)) return;
    sending.current = true;
    setPending(true);
    setCodeError(null);
    const result = await settle(
      fetchApi<ApproveResponse>(`/api/activity/approvals/${challenge.approval_id}/approve`, {
        method: 'POST',
        body: JSON.stringify({ code }),
      }),
    );
    setPending(false);
    sending.current = false;
    const d = 'err' in result ? parseErrorDetail(result.err) : null;
    if (d?.code === 'approval_expired') {
      setChallenge(null);
      setCode('');
      setCodeError(null);
      setFailure({
        tone: 'caution',
        title: `The one-time code expired at ${utc(challenge.expires_at) ?? 'its expiry'}. Nothing was placed.`,
        lines: ['Go back and review the order again for a fresh code.'],
        actions: [],
      });
      return;
    }
    const outcome = liveOrderOutcome(req, result, sid, 'approve');
    if ('ok' in result && result.ok.executed && !outcome.unanswered) {
      onPlaced();
      onOutcome(outcome);
      onSent();
      return;
    }
    if (outcome.inline) {
      setCodeError([outcome.title, ...outcome.lines].join(' '));
      return;
    }
    // The challenge is spent or its outcome unknown: no second order can leave this screen (R24).
    setChallenge(null);
    setCode('');
    setCodeError(null);
    setFailure(outcome);
  };

  if (down) {
    return (
      <div className="flex flex-col gap-3">
        <div role="alert" className="flex flex-col gap-1">
          <p className="text-sm font-semibold text-caution">
            <DataStateBadge state="unavailable" reason="The code service is unavailable" /> No order was placed.
          </p>
          <p className="text-sm text-ink-2">Live actions are hidden until the code service recovers.</p>
        </div>
        <div>
          <Button variant="secondary" onClick={onBack}>
            Back
          </Button>
        </div>
      </div>
    );
  }

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        void place();
      }}
    >
      <p className="font-semibold text-ink-1">{LIVE_FACTS}</p>
      <p className="rounded border border-live bg-live-soft p-3 text-sm text-ink-1">
        <b>{orderSentence(req, about)}</b> Session {sessionName(session)}.
      </p>
      {challenge ? (
        <>
          <p className="text-sm text-ink-1">
            One-time code:{' '}
            <span className="font-mono text-base font-semibold tracking-widest">{challenge.code}</span> — enter it once
            to place this order.
          </p>
          <p className="text-xs text-ink-2">The code expires {utc(challenge.expires_at) ?? '—'}.</p>
        </>
      ) : (
        <p className="text-sm text-ink-2">Getting the one-time code…</p>
      )}
      <Input
        label={`Type ${req.quantity} to confirm`}
        inputMode="decimal"
        autoComplete="off"
        value={typed}
        onChange={(e) => setTyped(e.target.value.trim())}
        hint="Type the quantity exactly as shown."
      />
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
      {failure && <Failure outcome={failure} />}
      <div className="flex flex-col gap-2 lg:flex-row-reverse">
        {challenge && (
          <Button
            type="submit"
            size="lg"
            className="max-lg:w-full"
            loading={pending}
            disabled={typed !== req.quantity || !CODE_RE.test(code)}
          >
            {pending ? 'Placing live order…' : 'Place live order'}
          </Button>
        )}
        <Button type="button" variant="secondary" size="lg" className="max-lg:w-full" onClick={onBack}>
          Back
        </Button>
      </div>
    </form>
  );
}
