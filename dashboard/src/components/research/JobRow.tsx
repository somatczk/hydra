'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { ApiError, fetchApi } from '@/lib/api';
import { describeApiError } from '@/lib/apiState';
import {
  fetchLine,
  jobAgeText,
  jobProgressText,
  jobStatusText,
  jobStatusTone,
  type JobKind,
  type ResearchJob,
  type Tone,
} from '@/lib/research';

interface JobRowProps {
  kind: JobKind;
  job: ResearchJob;
  /** Refetch the job list after a cancel: the row's own copy is stale by then. */
  onRefetch?: () => void;
  now?: number;
}

const BADGE_VARIANT: Record<Tone, 'error' | 'warning' | 'info'> = {
  danger: 'error',
  caution: 'warning',
  none: 'info',
};

/** One durable job: state, age, progress, verbatim error, cancel behind a confirm, links once completed. */
export function JobRow({ kind, job, onRefetch, now }: JobRowProps) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const cancelSent = useRef(false);

  const cancellable = job.status === 'queued' || job.status === 'running';
  useEffect(() => {
    if (!cancellable) setDialogOpen(false);
  }, [cancellable]);

  const ageText = jobAgeText(job.status, job.created_at, job.finished_at, now);
  const progressText = jobProgressText(kind, job);
  const meta = [job.name ? job.strategy_id : null, ageText, progressText, job.worker_id ? `worker ${job.worker_id}` : null]
    .filter(Boolean)
    .join(' · ');

  let resultHref: string | null = null;
  if (job.status === 'completed') {
    resultHref =
      kind === 'backtest'
        ? job.result_id
          ? `/backtest/${job.result_id}`
          : null
        : `/optimize/${job.task_id}`;
  }

  const openCancel = () => {
    cancelSent.current = false;
    setNotice(null);
    setDialogOpen(true);
  };

  const confirmCancel = async () => {
    if (cancelSent.current) return;
    cancelSent.current = true;
    try {
      await fetchApi(`/api/backtest/jobs/${job.task_id}/cancel`, { method: 'POST' });
    } catch (err) {
      setNotice(
        err instanceof ApiError && err.status === 409
          ? 'This run already finished.'
          : describeApiError(err, 'Cancel failed'),
      );
    }
    setDialogOpen(false);
    onRefetch?.();
  };

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-line bg-surface px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={jobStatusText(job.status)} variant={BADGE_VARIANT[jobStatusTone(job.status)]} />
          <p className="min-w-0 truncate text-sm font-medium text-ink-1">
            {job.name || job.strategy_id || job.task_id}
          </p>
        </div>
        {meta && <p className="mt-1 text-xs text-ink-2">{meta}</p>}
        {job.fetch && <p className="mt-1 text-xs text-ink-2" data-testid="job-fetch">{fetchLine(job.fetch)}</p>}
        {job.error && <p className="mt-1 text-xs text-danger">{job.error}</p>}
        {notice && <p className="mt-1 text-xs text-caution">{notice}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {resultHref && (
          <Link
            href={resultHref}
            className="inline-flex h-10 items-center rounded border border-line-strong bg-surface px-4 text-sm font-medium text-ink-1 hover:bg-hover max-lg:h-11"
          >
            {kind === 'backtest' ? 'View result' : 'View results'}
          </Link>
        )}
        {cancellable && (
          <Button variant="outline" onClick={openCancel}>
            Cancel
          </Button>
        )}
      </div>
      <ConfirmDialog
        open={dialogOpen}
        destructive
        title="Cancel this run?"
        primaryLabel="Cancel run"
        secondaryLabel="Keep running"
        pendingLabel="Cancelling…"
        pendingHint="Closing this won't undo the cancel."
        onConfirm={confirmCancel}
        onClose={() => setDialogOpen(false)}
      >
        <p>The worker stops at the next checkpoint. Anything already finished stays.</p>
      </ConfirmDialog>
    </div>
  );
}
