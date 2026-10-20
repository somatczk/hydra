import { describe, expect, it } from 'vitest';
import { ApiError } from './api';
import { describeApiError, isStale, parseErrorDetail, stateFromError } from './apiState';

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
