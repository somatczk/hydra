/**
 * The server is the single source of session state, so a fill or a status change (stop, error,
 * incomplete flatten) only triggers a re-fetch.
 */

export type SessionWsAction = 'refetch' | 'ignore';

const REFETCH_EVENTS = new Set(['order_fill', 'session_status']);

export function classifySessionWsMessage(msg: unknown, sessionId: string): SessionWsAction {
  if (typeof msg !== 'object' || msg === null) return 'ignore';
  const m = msg as Record<string, unknown>;
  return typeof m.event === 'string' && REFETCH_EVENTS.has(m.event) && m.session_id === sessionId
    ? 'refetch'
    : 'ignore';
}

export interface Debounced {
  trigger: () => void;
  cancel: () => void;
}

/** Collapse bursts of triggers into one call `waitMs` after the last one. */
export function debounce(fn: () => void, waitMs: number): Debounced {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return {
    trigger() {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        fn();
      }, waitMs);
    },
    cancel() {
      if (timer !== null) clearTimeout(timer);
      timer = null;
    },
  };
}
