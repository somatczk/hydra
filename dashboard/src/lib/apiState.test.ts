import { describe, expect, it } from 'vitest';
import { ApiError } from './api';
import {
  describeApiError,
  fetchAgeLabel,
  isSignedOut,
  isStale,
  parseErrorDetail,
  stateFromError,
} from './apiState';

function envelope(status: number, detail: unknown): ApiError {
  return new ApiError(status, JSON.stringify({ detail }));
}

describe('stateFromError', () => {
  it('reads the state from the server envelope', () => {
    const err = envelope(503, {
      code: 'database_unavailable',
      message: 'Database is not available',
      state: 'unavailable',
    });
    expect(stateFromError(err)).toBe('unavailable');
  });

  it('keeps not_found and no_data distinguishable', () => {
    expect(stateFromError(envelope(404, { code: 'x', message: 'm', state: 'not_found' }))).toBe(
      'not_found',
    );
    expect(stateFromError(envelope(503, { code: 'x', message: 'm', state: 'no_data' }))).toBe(
      'no_data',
    );
  });

  it('falls back to the HTTP status when the body is not an envelope', () => {
    expect(stateFromError(new ApiError(503, ''))).toBe('unavailable');
    expect(stateFromError(new ApiError(501, ''))).toBe('not_implemented');
    expect(stateFromError(new ApiError(500, 'Internal Server Error'))).toBe('unknown');
    expect(stateFromError(new ApiError(503, '<html>bad gateway</html>'))).toBe('unavailable');
  });

  it('maps auth refusals to their own states', () => {
    const unauth = envelope(401, {
      code: 'unauthenticated',
      message: 'Authentication required',
      state: 'unauthenticated',
    });
    expect(stateFromError(unauth)).toBe('unauthenticated');
    expect(describeApiError(unauth)).toBe('Authentication required');

    const csrf = envelope(403, {
      code: 'csrf_rejected',
      message: 'Missing X-Hydra-Request header',
      state: 'forbidden',
    });
    expect(parseErrorDetail(csrf)).toEqual({
      code: 'csrf_rejected',
      message: 'Missing X-Hydra-Request header',
      state: 'forbidden',
    });
    expect(stateFromError(csrf)).toBe('forbidden');
  });

  it('maps bare 401 and 403 responses by status', () => {
    expect(stateFromError(new ApiError(401, ''))).toBe('unauthenticated');
    expect(stateFromError(new ApiError(403, '<html>Forbidden</html>'))).toBe('forbidden');
    expect(parseErrorDetail(envelope(403, 'Not allowed'))?.state).toBe('forbidden');
  });

  it('treats network failures and plain errors as unknown', () => {
    expect(stateFromError(new TypeError('Failed to fetch'))).toBe('unknown');
    expect(stateFromError(null)).toBe('unknown');
    expect(stateFromError('boom')).toBe('unknown');
  });
});

describe('parseErrorDetail', () => {
  it('keeps extra envelope fields', () => {
    const detail = parseErrorDetail(
      envelope(422, {
        code: 'exchange_not_allowed',
        message: 'Order book is only available for: binance',
        state: 'invalid',
        allowed: ['binance'],
      }),
    );
    expect(detail).toEqual({
      code: 'exchange_not_allowed',
      message: 'Order book is only available for: binance',
      state: 'invalid',
      allowed: ['binance'],
    });
  });

  it('flattens FastAPI validation errors into one invalid message', () => {
    const err = envelope(422, [
      {
        loc: ['body', 'slack_webhook_url'],
        msg: 'Value error, webhook URL host must be hooks.slack.com',
        type: 'value_error',
      },
      { loc: ['body', 'paper_capital'], msg: 'Input should be greater than 0', type: 'greater_than' },
    ]);
    expect(parseErrorDetail(err)).toEqual({
      code: 'validation_error',
      message:
        'slack_webhook_url: Value error, webhook URL host must be hooks.slack.com; ' +
        'paper_capital: Input should be greater than 0',
      state: 'invalid',
    });
  });

  it('reads the server validation envelope', () => {
    const errors = [{ loc: ['body', 'trading_mode'], msg: "Input should be 'paper' or 'live'", type: 'literal_error' }];
    const err = envelope(422, {
      code: 'validation_error',
      message: "trading_mode: Input should be 'paper' or 'live'",
      state: 'invalid',
      errors,
    });
    expect(stateFromError(err)).toBe('invalid');
    expect(describeApiError(err)).toBe("trading_mode: Input should be 'paper' or 'live'");
    expect(parseErrorDetail(err)?.errors).toEqual(errors);
  });

  it('maps legacy string details by status', () => {
    expect(parseErrorDetail(envelope(404, 'Session abc not found'))).toEqual({
      code: 'http_404',
      message: 'Session abc not found',
      state: 'not_found',
    });
  });
});

describe('describeApiError', () => {
  it('prefers the server message', () => {
    const err = envelope(503, {
      code: 'risk_config_missing',
      message: "No risk_config row for scope 'global'",
      state: 'unavailable',
    });
    expect(describeApiError(err, 'Failed to load')).toBe("No risk_config row for scope 'global'");
  });

  it('falls back with the status code, never the raw body', () => {
    expect(describeApiError(new ApiError(500, '<html>oops</html>'), 'Failed to save')).toBe(
      'Failed to save (HTTP 500)',
    );
    expect(describeApiError(new TypeError('Failed to fetch'), 'Failed to save')).toBe(
      'Failed to save',
    );
  });
});

describe('isStale', () => {
  const now = Date.parse('2026-10-20T12:00:20Z');

  it('is stale when older than the limit', () => {
    expect(isStale('2026-10-20T12:00:00Z', 15_000, now)).toBe(true);
  });

  it('is fresh within the limit', () => {
    expect(isStale('2026-10-20T12:00:10Z', 15_000, now)).toBe(false);
  });

  it('is stale when the timestamp is missing or garbage', () => {
    expect(isStale(undefined, 15_000, now)).toBe(true);
    expect(isStale(null, 15_000, now)).toBe(true);
    expect(isStale('not-a-date', 15_000, now)).toBe(true);
  });
});

describe('isSignedOut', () => {
  it('is true for the security middleware 401 envelope (security.py:294)', () => {
    const err = new ApiError(
      401,
      JSON.stringify({
        detail: { code: 'unauthenticated', message: 'Authentication required', state: 'unauthenticated' },
      }),
    );
    expect(isSignedOut(err)).toBe(true);
  });

  it('is true for any 401, whatever its body', () => {
    expect(isSignedOut(new ApiError(401, '<!DOCTYPE html><title>Login - Authelia</title>'))).toBe(true);
  });

  it('known limit until E-1: a redirect to the sign-in page reaches us as a JSON SyntaxError, not a 401', () => {
    expect(isSignedOut(new SyntaxError('Unexpected token < in JSON'))).toBe(false);
  });

  it('is false for a 403, a 503 and a network TypeError', () => {
    expect(isSignedOut(new ApiError(403, '{}'))).toBe(false);
    expect(isSignedOut(new ApiError(503, '{}'))).toBe(false);
    expect(isSignedOut(new TypeError('Failed to fetch'))).toBe(false);
  });
});

describe('fetchAgeLabel', () => {
  const now = Date.parse('2026-10-21T14:04:10Z');

  it('says Fetching… while a poll is in flight', () => {
    expect(fetchAgeLabel(null, 'loading', now)).toBe('Fetching…');
    expect(fetchAgeLabel(now - 6_000, 'refreshing', now)).toBe('Fetching…');
  });

  it('gives the fetch age when ok and the as-of time when stale', () => {
    expect(fetchAgeLabel(now - 6_000, 'ok', now)).toBe('Fetched 6 s ago');
    expect(fetchAgeLabel(now - 120_000, 'stale', now)).toEqual({ stale: true, asOf: '14:02:10 UTC (2 min ago)' });
  });

  it('names a stale time 24 h or more old once, not twice', () => {
    expect(fetchAgeLabel(now - 3 * 86_400_000, 'stale', now)).toEqual({ stale: true, asOf: '18 Oct 14:04 UTC' });
  });

  it('has nothing to say when nothing was ever fetched', () => {
    expect(fetchAgeLabel(null, 'unavailable', now)).toBeNull();
  });
});
