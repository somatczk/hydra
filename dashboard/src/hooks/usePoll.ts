'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchApi } from '@/lib/api';
import { stateFromError, type PollState } from '@/lib/apiState';

export interface Poll<T> {
  data: T | null;
  error: unknown;
  state: PollState;
  fetchedAt: number | null;
  refetch: () => Promise<void>;
}

interface Snap<T> {
  path: string | null;
  data: T | null;
  error: unknown;
  state: PollState;
  fetchedAt: number | null;
  misses: number;
}

const initial = <T,>(path: string | null): Snap<T> => ({
  path,
  data: null,
  error: null,
  state: 'loading',
  fetchedAt: null,
  misses: 0,
});

/** One polled source: keeps the last value and turns STALE after 2 missed polls (05 §4). A null path pauses. */
export function usePoll<T>(path: string | null, intervalMs: number): Poll<T> {
  const [snap, setSnap] = useState<Snap<T>>(() => initial<T>(path));
  const current = useRef(path);

  const refetch = useCallback(async () => {
    if (path === null) return;
    setSnap((s) =>
      s.path !== path ? initial<T>(path) : s.data !== null && s.state === 'ok' ? { ...s, state: 'refreshing' } : s,
    );
    try {
      const data = await fetchApi<T>(path);
      if (current.current !== path) return;
      setSnap({ path, data, error: null, state: 'ok', fetchedAt: Date.now(), misses: 0 });
    } catch (error) {
      if (current.current !== path) return;
      setSnap((s) => {
        if (s.path !== path || s.data === null) return { ...initial<T>(path), error, state: stateFromError(error) };
        const misses = s.misses + 1;
        return { ...s, error, misses, state: misses >= 2 ? 'stale' : 'ok' };
      });
    }
  }, [path]);

  useEffect(() => {
    current.current = path;
    if (path === null) return;
    let timer: ReturnType<typeof setInterval> | undefined;
    const start = () => {
      clearInterval(timer);
      timer = setInterval(() => void refetch(), intervalMs);
    };
    const onVisibility = () => {
      if (document.hidden) {
        clearInterval(timer);
      } else {
        void refetch();
        start();
      }
    };
    void refetch();
    if (!document.hidden) start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [path, intervalMs, refetch]);

  const s = snap.path === path ? snap : initial<T>(path);
  return { data: s.data, error: s.error, state: s.state, fetchedAt: s.fetchedAt, refetch };
}
