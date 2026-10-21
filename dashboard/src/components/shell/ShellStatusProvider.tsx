'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { usePoll, type Poll } from '@/hooks/usePoll';
import { needsYou, type LiveStatus, type NeedsYou, type PlatformConfig, type SessionResponse } from '@/lib/derive';
import type { Outcome } from '@/lib/outcomes';

/** The last shell outcome; `ksOn` marks one that claims the kill switch is on (C-KS-6 watches it). */
export interface ShellOutcome {
  outcome: Outcome;
  at: number;
  ksOn: boolean;
}

export interface ShellStatus {
  live: Poll<LiveStatus>;
  sessions: Poll<SessionResponse[]>;
  config: Poll<PlatformConfig>;
  needs: NeedsYou;
  ksNotSaved: boolean;
  setKsNotSaved: (notSaved: boolean) => void;
  outcome: ShellOutcome | null;
  setOutcome: (outcome: Outcome | null, ksOn?: boolean) => void;
  refetchAll: () => Promise<void>;
  /** Ticks every 10 s so ages re-render no faster than that (R-P2-12). */
  now: number;
}

const ShellStatusContext = createContext<ShellStatus | null>(null);

export function useShellStatus(): ShellStatus {
  const ctx = useContext(ShellStatusContext);
  if (!ctx) throw new Error('useShellStatus must be used within a ShellStatusProvider');
  return ctx;
}

const DISAGREE: Outcome = {
  tone: 'caution',
  title: 'The engine now reports the kill switch as off.',
  lines: ['It may have been released elsewhere. Check the activity log.'],
  actions: [{ kind: 'open', label: 'Open activity log', href: '/risk#activity' }],
};

/** The only place live-status, the session list and the system config are polled. */
export function ShellStatusProvider({ children }: { children: ReactNode }) {
  const live = usePoll<LiveStatus>('/api/risk/live-status', 10_000);
  const sessions = usePoll<SessionResponse[]>('/api/trading/sessions', 10_000);
  const config = usePoll<PlatformConfig>('/api/system/config', 30_000);
  const [notSavedSince, setNotSavedSince] = useState<number | null>(null);
  const [outcome, setOutcomeState] = useState<ShellOutcome | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(timer);
  }, []);

  const ksState = live.data?.kill_switch_state;
  const liveAt = live.fetchedAt;
  // Only a live-status read taken after the outcome may overrule it.
  if (notSavedSince !== null && liveAt !== null && liveAt > notSavedSince && ksState !== 'active') {
    setNotSavedSince(null);
  }
  if (outcome?.ksOn && liveAt !== null && liveAt > outcome.at && ksState === 'inactive') {
    setOutcomeState({ outcome: DISAGREE, at: liveAt, ksOn: false });
  }

  const setKsNotSaved = useCallback((notSaved: boolean) => setNotSavedSince(notSaved ? Date.now() : null), []);
  const setOutcome = useCallback(
    (o: Outcome | null, ksOn = false) => setOutcomeState(o ? { outcome: o, at: Date.now(), ksOn } : null),
    [],
  );

  const { refetch: refetchLive } = live;
  const { refetch: refetchSessions } = sessions;
  const { refetch: refetchConfig } = config;
  const refetchAll = useCallback(async () => {
    await Promise.all([refetchLive(), refetchSessions(), refetchConfig()]);
  }, [refetchLive, refetchSessions, refetchConfig]);

  const ksNotSaved = notSavedSince !== null;
  const needs = useMemo(
    () => needsYou({ sessions: sessions.data, live: live.data, config: config.data, ksNotSaved }),
    [sessions.data, live.data, config.data, ksNotSaved],
  );

  return (
    <ShellStatusContext.Provider
      value={{ live, sessions, config, needs, ksNotSaved, setKsNotSaved, outcome, setOutcome, refetchAll, now }}
    >
      {children}
    </ShellStatusContext.Provider>
  );
}
