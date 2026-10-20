/**
 * Session-page WebSocket handling. The server's session detail endpoint is the
 * single source of PnL and win rate, so a fill only triggers a re-fetch.
 */

export type SessionWsAction = 'refetch' | 'ignore';

export function classifySessionWsMessage(msg: unknown, sessionId: string): SessionWsAction {
  if (typeof msg !== 'object' || msg === null) return 'ignore';
  const m = msg as Record<string, unknown>;
  return m.event === 'order_fill' && m.session_id === sessionId ? 'refetch' : 'ignore';
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
