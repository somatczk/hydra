/**
 * Hydra Dashboard API client.
 *
 * Provides `fetchApi` for REST calls and `connectWebSocket` for streaming data.
 * Falls back gracefully when the API is unreachable.
 */

import { logger } from './logger';

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ||
  (typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8000');

/** NEXT_PUBLIC_WS_URL lets local dev reach the engine's WebSockets while REST stays same-origin. */
const WS_BASE = (
  process.env.NEXT_PUBLIC_WS_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  (typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8000')
).replace(/^http/, 'ws');

// ---------------------------------------------------------------------------
// REST helper
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/** The engine refuses non-GET browser requests without this header (CSRF guard). */
export const CSRF_HEADER = 'x-hydra-request';

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

/** Headers for a raw `fetch` to the API (e.g. a FormData upload) that bypasses `fetchApi`. */
export function apiHeaders(method = 'POST'): Record<string, string> {
  return SAFE_METHODS.has(method.toUpperCase()) ? {} : { [CSRF_HEADER]: '1' };
}

function callerHeaders(headers: HeadersInit | undefined): Record<string, string> {
  if (!headers) return {};
  return Object.fromEntries(new Headers(headers).entries());
}

/** Envelope for a request the auth proxy redirected to its sign-in page (E-1, R-P2-13). */
const SIGNED_OUT_BODY = JSON.stringify({
  detail: { code: 'signed_out', message: "You're signed out. Sign in, then try again.", state: 'unauthenticated' },
});

/**
 * Typed fetch wrapper for the Hydra API.
 *
 * @param path  - API path starting with `/` (e.g. `/api/strategies`)
 * @param options - Standard `RequestInit` overrides; caller headers are merged, not replaced
 * @returns Parsed JSON response typed as `T`
 * @throws {ApiError} when the server returns a non-2xx status, or 401 when sent to sign-in
 */
export async function fetchApi<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const method = (options?.method ?? 'GET').toUpperCase();
  const url = `${API_BASE}${path}`;
  const t0 = performance.now();
  const headers: Record<string, string> = {
    // Authelia answers 401, not a redirect to its login page, to a request that doesn't accept HTML.
    accept: 'application/json',
    ...(typeof options?.body === 'string' ? { 'content-type': 'application/json' } : {}),
    ...apiHeaders(method),
    ...callerHeaders(options?.headers),
  };

  let res: Response;
  try {
    // A cross-origin redirect to the sign-in portal would reject as a CORS TypeError; keep it readable instead.
    res = await fetch(url, { redirect: 'manual', ...options, method, headers });
  } catch (err) {
    const ms = Math.round(performance.now() - t0);
    logger.error('API', `${method} ${path} NETWORK_ERROR (${ms}ms)`, err);
    throw err;
  }

  const ms = Math.round(performance.now() - t0);

  const loginPage = res.ok && (res.headers.get('content-type') ?? '').includes('text/html');
  if (loginPage || res.type === 'opaqueredirect') {
    logger.error('API', `${method} ${path} -> redirected to sign-in (${ms}ms)`);
    throw new ApiError(401, SIGNED_OUT_BODY);
  }

  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    logger.error('API', `${method} ${path} -> ${res.status} (${ms}ms)`, text);
    throw new ApiError(res.status, text);
  }

  logger.info('API', `${method} ${path} -> ${res.status} (${ms}ms)`);

  if (res.status === 204) {
    return undefined as T;
  }

  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// WebSocket helper
// ---------------------------------------------------------------------------

/**
 * Connect to a Hydra WebSocket channel.
 *
 * This is a plain function (not a React hook) that creates a WebSocket.
 * Safe to call from useEffect callbacks or event handlers.
 *
 * @param path      - WS path starting with `/` (e.g. `/ws/market`)
 * @param onMessage - Callback invoked with each parsed JSON message
 * @returns The WebSocket instance (or `null` if connection fails)
 */
export function connectWebSocket(
  path: string,
  onMessage: (data: unknown) => void,
): WebSocket | null {
  try {
    logger.info('WS', `Connecting to ${path}`);
    const ws = new WebSocket(`${WS_BASE}${path}`);

    ws.onopen = () => {
      logger.info('WS', `Connected to ${path}`);
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        onMessage(data);
      } catch {
        logger.warn('WS', `Non-JSON message on ${path}`);
      }
    };

    ws.onerror = () => {
      logger.warn('WS', `Error on ${path}`);
    };

    ws.onclose = (event) => {
      logger.info('WS', `Disconnected from ${path} (code=${event.code})`);
    };

    return ws;
  } catch (err) {
    logger.error('WS', `Failed to connect to ${path}`, err);
    return null;
  }
}
