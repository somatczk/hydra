'use client';

import { useEffect, useRef, useState } from 'react';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { Button } from '@/components/ui/Button';
import { ApiError, fetchApi } from '@/lib/api';
import { describeApiError, parseErrorDetail } from '@/lib/apiState';
import type { WireSchedule } from '@/lib/schedule';

/** `GET /api/strategies/{id}` drift fields (services.strategies._session_drift). */
export interface StrategyDrift {
  config_hash: string | null;
  file_changed: boolean;
  running_sessions: Array<{ session_id: string; config_hash: string | null; stale: boolean }>;
  /** The stored entry schedule; null or absent is always active. */
  schedule?: WireSchedule | null;
}

/** `POST /api/strategies/{id}/reload` 200 body (services.strategies.ReloadResponse). */
interface ReloadOk {
  session_id: string;
  from_hash: string | null;
  to_hash: string;
}

interface StrategyDriftPanelProps {
  strategyId: string;
  drift: StrategyDrift;
  /** Refetch the strategy detail after a reload: the panel's copy of `drift` is stale by then. */
  onRefetch: () => void;
}

interface DriftNotice {
  tone: 'ok' | 'caution' | 'danger';
  text: string;
}

/** A reload the API accepted; the detail keeps calling the session stale until it lands. */
interface AppliedReload {
  sessionId: string;
  toHash: string;
}

/** The file-changed caution row and the parameters-only reload (08 §4); C-STRAT-CHANGED / C-RELOAD-1..4. */
export function StrategyDriftPanel({ strategyId, drift, onRefetch }: StrategyDriftPanelProps) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [notice, setNotice] = useState<DriftNotice | null>(null);
  const [applied, setApplied] = useState<AppliedReload | null>(null);
  const reloadSent = useRef(false);

  // A 200 only queues the reload for the next closed bar, so the refetched detail still calls the
  // session stale; suppress that known-pending row until the session reports the new hash or stops.
  const pending = (s: StrategyDrift['running_sessions'][number]) =>
    applied !== null && s.session_id === applied.sessionId && s.config_hash !== applied.toHash;

  useEffect(() => {
    if (applied === null) return;
    const entry = drift.running_sessions.find((s) => s.session_id === applied.sessionId);
    if (entry === undefined || entry.config_hash === applied.toHash) {
      setApplied(null);
      setNotice(null);
    }
  }, [drift, applied]);

  const drifted =
    applied === null
      ? drift.file_changed || drift.running_sessions.some((s) => s.stale)
      : drift.running_sessions.some((s) => s.stale && !pending(s));
  if (!drifted && notice === null) return null;

  const openDialog = () => {
    reloadSent.current = false;
    setNotice(null);
    setDialogOpen(true);
  };

  const confirmReload = async () => {
    if (reloadSent.current) return;
    reloadSent.current = true;
    try {
      const ok = await fetchApi<ReloadOk>(`/api/strategies/${strategyId}/reload`, { method: 'POST' });
      setNotice({ tone: 'ok', text: 'Applied. The new parameters take effect at the next closed bar.' });
      if (typeof ok?.session_id === 'string' && typeof ok?.to_hash === 'string') {
        setApplied({ sessionId: ok.session_id, toHash: ok.to_hash });
      }
    } catch (err) {
      const detail = parseErrorDetail(err);
      if (err instanceof ApiError && err.status === 409) {
        const fields = Array.isArray(detail?.changed_fields) && detail.changed_fields.length > 0
          ? detail.changed_fields.map(String).join(', ')
          : detail?.message ?? '';
        setNotice({
          tone: 'danger',
          text: `A restart is needed to apply: ${fields}. Nothing was applied.`,
        });
      } else if (err instanceof ApiError && err.status === 404) {
        // strategy_not_found: the file is gone while a session runs, so the frozen no-session sentence would be false.
        if (detail?.code === 'strategy_not_found' && detail.message) {
          setNotice({ tone: 'danger', text: detail.message });
        } else {
          setNotice({ tone: 'caution', text: 'No running session for this strategy.' });
        }
      } else if (err instanceof ApiError && err.status === 422 && detail?.message) {
        setNotice({ tone: 'danger', text: detail.message });
      } else {
        setNotice({ tone: 'caution', text: describeApiError(err, 'Apply failed') });
      }
    }
    reloadSent.current = false;
    setDialogOpen(false);
    onRefetch();
  };

  const toneClass = notice?.tone === 'danger' ? 'text-danger' : notice?.tone === 'caution' ? 'text-caution' : 'text-ink-1';

  return (
    <div className="mt-3 flex flex-col gap-2 rounded-lg border border-line bg-surface p-3" data-testid="strategy-drift">
      {drifted && (
        <>
          <p className="text-xs text-caution">The strategy file changed after this session started.</p>
          <Button variant="outline" size="sm" className="self-start" onClick={openDialog}>
            Apply to session…
          </Button>
        </>
      )}
      {notice && (
        <p role="status" className={`text-xs ${toneClass}`}>
          {notice.text}
        </p>
      )}
      <ConfirmDialog
        open={dialogOpen}
        destructive
        title="Apply the changed parameters?"
        primaryLabel="Apply to session"
        secondaryLabel="Not now"
        pendingLabel="Applying…"
        pendingHint="Closing this won't cancel the apply."
        onConfirm={confirmReload}
        onClose={() => setDialogOpen(false)}
      >
        <p>
          The changed <b>parameters</b> take effect at the next closed bar. Changes to rules, risk, sizing, symbols or
          the timeframe are refused; they need a session restart.
        </p>
      </ConfirmDialog>
    </div>
  );
}
