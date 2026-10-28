'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ActivityLog, type RiskEvent } from './ActivityLog';
import { Button } from '@/components/ui/Button';
import { DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { Input } from '@/components/ui/Input';
import { Section } from '@/components/ui/Section';
import { actionPhrase, askPhrase, type ApprovalDetail } from '@/lib/derive';
import { liveUnknownLine } from '@/lib/outcomes';
import { duration, utc } from '@/lib/format';

const CODE_LENGTH = 6;
const CODE_RE = /^\d{6}$/;

export interface ApprovalReviewProps {
  approval: ApprovalDetail;
  /** The asker's last actions (`?actor={actor}`); null while loading. */
  trail: RiskEvent[] | null;
  /** Set when the last trail read failed (then `trail` is null). */
  trailError: unknown;
  onRetryTrail: () => void;
  /** Ticks at most every 10 s; the expiry line is plain static text between ticks, never a live region. */
  now: number;
  /** The active breaker tier from the shell's live-status poll; the release facts name it (as /risk does). */
  activeTier: number;
  busy: 'approve' | 'deny' | null;
  /** The last failed attempt, shown inline; the typed code stays. */
  error: string | null;
  /** Set when this page made the decision (then the copy names this device, not another). */
  decidedHere: 'approved' | 'denied' | null;
  onApprove: (code: string) => void;
  onDeny: () => void;
}

/** F8: What (the ask), Why (the asker's own words), Context, the asker's trail, the expiry, the one-time code. */
export function ApprovalReview({
  approval,
  trail,
  trailError,
  onRetryTrail,
  now,
  activeTier,
  busy,
  error,
  decidedHere,
  onApprove,
  onDeny,
}: ApprovalReviewProps) {
  const [code, setCode] = useState('');
  const valid = CODE_RE.test(code);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const fieldRef = useRef<HTMLInputElement>(null);
  const hadValid = useRef(false);
  const hadError = useRef(false);

  // S11: the primary takes focus only once the code is valid; Deny stays Tab-reachable without one.
  useEffect(() => {
    if (valid && !hadValid.current) primaryRef.current?.focus();
    hadValid.current = valid;
  }, [valid]);

  // A3: a rejected code leaves the field — the recovery surface — focused, so Enter can't re-send it.
  useEffect(() => {
    if (error && !hadError.current) fieldRef.current?.focus();
    hadError.current = error !== null;
  }, [error]);

  const action = actionPhrase(approval.action);
  const isClaude = approval.actor === 'service:mcp';
  const pending = approval.status === 'pending';
  const at = (iso: string | null) => utc(iso, now) ?? 'time not reported';
  const decided = pending
    ? null
    : decidedHere === 'approved'
      ? `You approved this at ${at(approval.decided_at)}.`
      : decidedHere === 'denied'
        ? `You denied this at ${at(approval.decided_at)}.`
        : approval.status === 'approved'
          ? `You approved this at ${at(approval.decided_at)} on another device.`
          : approval.status === 'denied'
            ? `You denied this at ${at(approval.decided_at)} on another device.`
            : approval.status === 'expired'
              ? `This request expired at ${at(approval.expires_at)}. Nothing changed.`
              : approval.status === 'failed'
                ? // R24: a live order or start may exist unwatched — never "Ask for a new one."
                  [
                    'This approval was invalidated: too many wrong codes, or the action failed to run.',
                    liveUnknownLine(approval.action) ?? 'Ask for a new one.',
                  ].join(' ')
                : 'This approval is no longer waiting for you.';

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-col gap-2">
        <h1 className="text-xl font-semibold text-ink-1">{askPhrase(approval.actor, approval.action)}</h1>
        <p className="text-sm text-ink-1">{isClaude ? 'Claude' : 'You'} wrote: “{approval.reason}”</p>
        {decided && <p className="text-sm font-medium text-ink-1">{decided}</p>}
        {approval.status === 'failed' && approval.error && <p className="text-sm text-ink-2">{approval.error}</p>}
      </header>

      <Section title="Context">
        {approval.action === 'kill_switch_release' && (
          <p className="mb-2 text-sm text-ink-1">
            Releasing lets sessions start and open positions again. It doesn&apos;t restart stopped sessions.
            {activeTier > 0 && ` Breaker tier ${activeTier} is still active and still blocks entries; reset it separately.`}
          </p>
        )}
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-ink-2">Action</dt>
          <dd className="font-mono text-ink-1">{approval.action}</dd>
          <dt className="text-ink-2">Reason</dt>
          <dd className="text-ink-1">{approval.reason}</dd>
          <dt className="text-ink-2">Params</dt>
          <dd>
            <pre className="whitespace-pre-wrap break-words font-mono text-ink-1">{JSON.stringify(approval.params)}</pre>
          </dd>
        </dl>
      </Section>

      <Section title={isClaude ? "Claude's recent activity" : 'Your recent activity'}>
        {trailError !== null && trailError !== undefined ? (
          <DataStateErrorCard
            error={trailError}
            fallback={isClaude ? "Claude's recent activity couldn't be read." : "Your recent activity couldn't be read."}
            onRetry={onRetryTrail}
          />
        ) : trail === null ? (
          <p className="text-sm text-ink-2">Loading…</p>
        ) : (
          <ActivityLog events={trail} now={now} />
        )}
      </Section>

      {pending ? (
        <Section title="Decide">
          <div className="flex flex-col gap-3">
            <p className="text-sm text-ink-2">Expires in {duration(Date.parse(approval.expires_at) - now) ?? '—'}</p>
            <Input
              ref={fieldRef}
              label="One-time code"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, CODE_LENGTH))}
              autoComplete="one-time-code"
              inputMode="numeric"
              pattern="[0-9]*"
              maxLength={CODE_LENGTH}
              hint={isClaude ? 'The 6 digits Claude shows you. Single use.' : 'The 6 digits shown to you. Single use.'}
              error={error ?? undefined}
              className="font-mono tracking-widest"
            />
            <div className="flex flex-col gap-2 lg:flex-row-reverse">
              <Button
                ref={primaryRef}
                size="lg"
                className="max-lg:w-full"
                disabled={!valid || busy !== null}
                loading={busy === 'approve'}
                onClick={() => onApprove(code)}
              >
                Approve and {action}
              </Button>
              <Button
                variant="ghost"
                size="lg"
                className="max-lg:w-full"
                disabled={busy !== null}
                loading={busy === 'deny'}
                onClick={onDeny}
              >
                Deny
              </Button>
            </div>
            <p className="text-xs text-ink-2">
              {isClaude
                ? 'Approving runs it as Claude, recorded as approved by you.'
                : 'Approving runs it now, recorded as approved by you.'}{' '}
              <Link href="/activity" className="text-accent hover:underline">
                Activity ›
              </Link>
            </p>
          </div>
        </Section>
      ) : (
        <p className="text-sm text-ink-2">
          Nothing to decide here.{' '}
          <Link href="/activity" className="text-accent hover:underline">
            Activity ›
          </Link>
        </p>
      )}
    </div>
  );
}
