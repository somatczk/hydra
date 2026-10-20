/** Shared reading of the dashboard API error envelope; no `@/` imports so it stays testable. */

export type ServerErrorState =
  | 'unavailable'
  | 'no_data'
  | 'not_found'
  | 'invalid'
  | 'not_implemented';

export type DataState = 'ok' | 'stale' | 'unknown' | ServerErrorState;

export interface ApiErrorDetail {
  code: string;
  message: string;
  state: ServerErrorState;
  [extra: string]: unknown;
}

interface HttpErrorLike {
  status: number;
  message: string;
}

const SERVER_STATES: ReadonlySet<string> = new Set([
  'unavailable',
  'no_data',
  'not_found',
  'invalid',
  'not_implemented',
]);

function isHttpError(err: unknown): err is HttpErrorLike {
  return (
    typeof err === 'object' &&
    err !== null &&
    typeof (err as { status?: unknown }).status === 'number' &&
    typeof (err as { message?: unknown }).message === 'string'
  );
}

function stateForStatus(status: number): ServerErrorState | null {
  if (status === 502 || status === 503 || status === 504) return 'unavailable';
  if (status === 501) return 'not_implemented';
  if (status === 404) return 'not_found';
  if (status === 400 || status === 422) return 'invalid';
  return null;
}

function validationMessage(items: unknown[]): string {
  const parts = items.map((item) => {
    if (typeof item !== 'object' || item === null) return String(item);
    const { loc, msg } = item as { loc?: unknown; msg?: unknown };
    const field = Array.isArray(loc) ? loc.filter((p) => p !== 'body').join('.') : '';
    const text = typeof msg === 'string' ? msg : 'invalid value';
    return field ? `${field}: ${text}` : text;
  });
  return parts.join('; ');
}

/** Parse the server's error detail out of an ApiError (or anything shaped like one). */
export function parseErrorDetail(err: unknown): ApiErrorDetail | null {
  if (!isHttpError(err)) return null;
  let body: unknown;
  try {
    body = JSON.parse(err.message);
  } catch {
    return null;
  }
  if (typeof body !== 'object' || body === null || !('detail' in body)) return null;
  const detail = (body as { detail: unknown }).detail;

  if (typeof detail === 'object' && detail !== null && !Array.isArray(detail)) {
    const d = detail as Record<string, unknown>;
    if (typeof d.state === 'string' && SERVER_STATES.has(d.state)) {
      return {
        ...d,
        code: typeof d.code === 'string' ? d.code : `http_${err.status}`,
        message: typeof d.message === 'string' ? d.message : '',
        state: d.state as ServerErrorState,
      };
    }
    return null;
  }
  if (Array.isArray(detail)) {
    return { code: 'validation_error', message: validationMessage(detail), state: 'invalid' };
  }
  if (typeof detail === 'string') {
    return {
      code: `http_${err.status}`,
      message: detail,
      state: stateForStatus(err.status) ?? 'unavailable',
    };
  }
  return null;
}

/** Map any thrown value to the data state a widget should show. */
export function stateFromError(err: unknown): DataState {
  const detail = parseErrorDetail(err);
  if (detail) return detail.state;
  if (isHttpError(err)) return stateForStatus(err.status) ?? 'unknown';
  return 'unknown';
}

/** Human-readable message for a toast or error card. */
export function describeApiError(err: unknown, fallback = 'Request failed'): string {
  const detail = parseErrorDetail(err);
  if (detail?.message) return detail.message;
  if (isHttpError(err)) return `${fallback} (HTTP ${err.status})`;
  return fallback;
}

/** True when the timestamp is missing, unparseable or older than maxAgeMs. */
export function isStale(
  tsIso: string | null | undefined,
  maxAgeMs: number,
  now: number = Date.now(),
): boolean {
  if (!tsIso) return true;
  const ts = Date.parse(tsIso);
  if (Number.isNaN(ts)) return true;
  return now - ts > maxAgeMs;
}
