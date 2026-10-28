'use client';

import { useRef, useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { ApprovalReview } from '@/components/activity/ApprovalReview';
import type { RiskEvent } from '@/components/activity/ActivityLog';
import { OutcomePanel } from '@/components/shell/OutcomePanel';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { usePoll } from '@/hooks/usePoll';
import { fetchApi } from '@/lib/api';
import { parseErrorDetail, stateFromError } from '@/lib/apiState';
import type { ApprovalDetail } from '@/lib/derive';
import {
  approvalOutcome,
  type ApproveResponse,
  type DenyResponse,
  type Outcome,
  type Result,
} from '@/lib/outcomes';

const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) });

async function settle<T>(p: Promise<T>): Promise<Result<T>> {
  try {
    return { ok: await p };
  } catch (err) {
    return { err };
  }
}

/** The store answers 409/410 when the decision already happened elsewhere; a refetch shows that state. */
const DECIDED_ELSEWHERE = new Set(['already_decided', 'approval_expired', 'approval_failed']);

export default function ApprovalReviewPage() {
  const { id } = useParams<{ id: string }>();
  const shell = useShellStatus();
  const detail = usePoll<ApprovalDetail>(`/api/activity/approvals/${id}`, 15_000);
  const trail = usePoll<RiskEvent[]>(`/api/activity?actor=service:mcp&limit=10`, 30_000);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [inline, setInline] = useState<string | null>(null);
  const [busy, setBusy] = useState<'approve' | 'deny' | null>(null);
  const [decidedHere, setDecidedHere] = useState<'approved' | 'denied' | null>(null);
  const last = useRef<{ kind: 'approve'; code: string } | { kind: 'deny' } | null>(null);
  /** A4: an approve whose answer never came back may have won; a later 409 already_decided says it did. */
  const approveUnanswered = useRef(false);

  const refresh = () => Promise.all([detail.refetch(), trail.refetch(), shell.refetchAll()]);

  const decidedElsewhere = (err: unknown) => {
    if (DECIDED_ELSEWHERE.has(parseErrorDetail(err)?.code ?? '')) {
      setOutcome(null);
      void detail.refetch();
      return true;
    }
    return false;
  };

  const approve = async (code: string) => {
    last.current = { kind: 'approve', code };
    setBusy('approve');
    const result = await settle(
      fetchApi<ApproveResponse>(`/api/activity/approvals/${id}/approve`, { method: 'POST', ...json({ code }) }),
    );
    setBusy(null);
    if ('ok' in result) {
      approveUnanswered.current = false;
      setInline(null);
      setDecidedHere('approved');
      setOutcome(approvalOutcome(result, 'approve', detail.data?.action));
      void refresh();
      return;
    }
    if (decidedElsewhere(result.err)) {
      // A 409 after our own unanswered approve means that earlier POST won, not another device.
      if (parseErrorDetail(result.err)?.code === 'already_decided' && approveUnanswered.current) setDecidedHere('approved');
      approveUnanswered.current = false;
      return;
    }
    const o = approvalOutcome(result, 'approve', detail.data?.action);
    approveUnanswered.current = o.unanswered === true;
    if (o.inline) setInline([o.title, ...o.lines].join(' '));
    else {
      setInline(null);
      setOutcome(o);
    }
    void refresh();
  };

  const deny = async () => {
    last.current = { kind: 'deny' };
    setBusy('deny');
    const result = await settle(fetchApi<DenyResponse>(`/api/activity/approvals/${id}/deny`, { method: 'POST', ...json({}) }));
    setBusy(null);
    if ('ok' in result) {
      setInline(null);
      setDecidedHere('denied');
      setOutcome(approvalOutcome(result));
      void refresh();
      return;
    }
    if (decidedElsewhere(result.err)) return;
    setOutcome(approvalOutcome(result, 'deny', detail.data?.action));
    void refresh();
  };

  const runLast = () => {
    const l = last.current;
    if (l?.kind === 'approve') void approve(l.code);
    else if (l?.kind === 'deny') void deny();
  };

  if (detail.data === null) {
    if (detail.state === 'loading') {
      return <div className="h-48 animate-pulse rounded border border-line bg-surface" aria-label="Loading approval" />;
    }
    if (stateFromError(detail.error) === 'not_found') {
      return (
        <div className="flex flex-col gap-4">
          <h1 className="text-xl font-semibold text-ink-1">No such approval</h1>
          <p className="text-sm text-ink-2">
            The engine keeps approvals in memory: a restart drops them, and a decided one is kept for an hour. Ask
            Claude to request it again.
          </p>
          <Link href="/activity" className="text-sm text-accent hover:underline">
            Activity ›
          </Link>
        </div>
      );
    }
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-xl font-semibold text-ink-1">Approval</h1>
        <DataStateErrorCard error={detail.error} fallback="The approval couldn't be loaded" onRetry={() => void detail.refetch()} />
        <Link href="/activity" className="text-sm text-accent hover:underline">
          Activity ›
        </Link>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <OutcomePanel
        outcome={outcome}
        onAction={() => runLast()}
        onDismiss={() => setOutcome(null)}
      />
      <ApprovalReview
        approval={detail.data}
        trail={trail.data}
        trailError={trail.data === null && trail.state !== 'loading' ? trail.error : null}
        onRetryTrail={() => void trail.refetch()}
        now={shell.now}
        activeTier={shell.live.data?.circuit_breaker_restrictions?.active_tier ?? 0}
        busy={busy}
        error={inline}
        decidedHere={decidedHere}
        onApprove={(code) => void approve(code)}
        onDeny={() => void deny()}
      />
    </div>
  );
}
