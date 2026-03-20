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

const WS_BASE = (
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

/**
 * Typed fetch wrapper for the Hydra API.
 *
 * @param path  - API path starting with `/` (e.g. `/api/strategies`)
 * @param options - Standard `RequestInit` overrides
 * @returns Parsed JSON response typed as `T`
 * @throws {ApiError} when the server returns a non-2xx status
 */
export async function fetchApi<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const method = options?.method ?? 'GET';
  const url = `${API_BASE}${path}`;
  const t0 = performance.now();

  let res: Response;
  try {
    res = await fetch(url, {
      headers: {
        'Content-Type': 'application/json',
        ...(options?.headers as Record<string, string> | undefined),
      },
      ...options,
    });
  } catch (err) {
    const ms = Math.round(performance.now() - t0);
    logger.error('API', `${method} ${path} NETWORK_ERROR (${ms}ms)`, err);
    throw err;
  }

  const ms = Math.round(performance.now() - t0);

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
