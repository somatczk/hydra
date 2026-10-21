'use client';

import { useRef, useState, type ReactNode } from 'react';
import { OutcomePanel } from '@/components/shell/OutcomePanel';
import { fetchApi } from '@/lib/api';
import type { SessionResponse } from '@/lib/derive';
import { stopOutcome, tryClosingOutcome, type Outcome, type OutcomeAction, type Result } from '@/lib/outcomes';

export async function settle<T>(p: Promise<T>): Promise<Result<T>> {
  try {
    return { ok: await p };
  } catch (err) {
    return { err };
  }
}

const deleteSession = (id: string) =>
  settle(fetchApi<SessionResponse>(`/api/trading/sessions/${id}`, { method: 'DELETE' }));

/** A page's own outcome slot (always mounted) plus the stop request that feeds it; C-STOP-2/3/4 and "Try closing again". */
export function useOutcomeSlot(nameOf: (id: string) => string, onChange: () => void) {
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState<OutcomeAction['kind'] | null>(null);
  const lastStop = useRef<string | null>(null);

  const stop = async (id: string) => {
    lastStop.current = id;
    setOutcome(stopOutcome(await deleteSession(id)));
    onChange();
  };

  const again = async (action: OutcomeAction) => {
    const ids = action.sessionIds ?? (lastStop.current ? [lastStop.current] : []);
    if (busy || ids.length === 0) return;
    setBusy(action.kind);
    try {
      const results = await Promise.all(ids.map(async (id) => ({ id, name: nameOf(id), result: await deleteSession(id) })));
      setOutcome(tryClosingOutcome(results));
      onChange();
    } finally {
      setBusy(null);
    }
  };

  const panel: ReactNode = (
    <OutcomePanel outcome={outcome} busy={busy} onAction={(a) => void again(a)} onDismiss={() => setOutcome(null)} />
  );
  return { outcome, setOutcome, stop, panel };
}
