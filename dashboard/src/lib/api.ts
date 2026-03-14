/**
 * Hydra Dashboard API client.
 *
 * Provides `fetchApi` for REST calls and `useWebSocket` for streaming data.
 * Falls back gracefully when the API is unreachable.
 */

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
  const url = `${API_BASE}${path}`;
  const res = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...(options?.headers as Record<string, string> | undefined),
    },
    ...options,
  });

  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new ApiError(res.status, text);
  }

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
 * @param path      - WS path starting with `/` (e.g. `/ws/market`)
 * @param onMessage - Callback invoked with each parsed JSON message
 * @returns The WebSocket instance (or `null` if connection fails)
 */
export function useWebSocket(
  path: string,
  onMessage: (data: unknown) => void,
): WebSocket | null {
  try {
    const ws = new WebSocket(`${WS_BASE}${path}`);

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        onMessage(data);
      } catch {
        // Non-JSON message; ignore
      }
    };

    ws.onerror = () => {
      // Silently handle -- dashboard degrades gracefully
    };

    return ws;
  } catch {
    return null;
  }
}
