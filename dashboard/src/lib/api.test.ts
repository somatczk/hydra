import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, apiHeaders, fetchApi } from './api';
import { isSignedOut, stateFromError } from './apiState';

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockResolvedValue(new Response('{"ok":true}', { status: 200 }));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

function sentHeaders(): Headers {
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  return new Headers(init.headers);
}

describe('fetchApi headers', () => {
  it('sends the CSRF header and JSON content type on a POST', async () => {
    await fetchApi('/api/trading/kill-switch', { method: 'POST', body: '{}' });
    const headers = sentHeaders();
    expect(headers.get('x-hydra-request')).toBe('1');
    expect(headers.get('content-type')).toBe('application/json');
  });

  it.each(['PUT', 'PATCH', 'DELETE', 'post'])('sends the CSRF header on %s', async (method) => {
    await fetchApi('/api/x', { method });
    expect(sentHeaders().get('x-hydra-request')).toBe('1');
  });

  it('keeps the CSRF header when the caller passes its own headers', async () => {
    await fetchApi('/api/x', {
      method: 'POST',
      body: 'a,b',
      headers: { 'Content-Type': 'text/csv', 'X-Trace': 'abc' },
    });
    const headers = sentHeaders();
    expect(headers.get('x-hydra-request')).toBe('1');
    expect(headers.get('content-type')).toBe('text/csv');
    expect(headers.get('x-trace')).toBe('abc');
  });

  it('accepts a Headers instance from the caller', async () => {
    await fetchApi('/api/x', { method: 'DELETE', headers: new Headers({ 'X-Trace': 't' }) });
    const headers = sentHeaders();
    expect(headers.get('x-hydra-request')).toBe('1');
    expect(headers.get('x-trace')).toBe('t');
  });

  it('adds no CSRF header or content type to a plain GET', async () => {
    await fetchApi('/api/x');
    const headers = sentHeaders();
    expect(headers.get('x-hydra-request')).toBeNull();
    expect(headers.get('content-type')).toBeNull();
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('GET');
  });

  it('lets the browser set the multipart boundary for FormData bodies', async () => {
    await fetchApi('/api/models/upload', { method: 'POST', body: new FormData() });
    const headers = sentHeaders();
    expect(headers.get('content-type')).toBeNull();
    expect(headers.get('x-hydra-request')).toBe('1');
  });

  it('surfaces a 403 envelope as ApiError with the raw body', async () => {
    const body = JSON.stringify({
      detail: { code: 'csrf_rejected', message: 'Origin is not allowed', state: 'forbidden' },
    });
    fetchMock.mockResolvedValueOnce(new Response(body, { status: 403 }));
    await expect(fetchApi('/api/x', { method: 'POST' })).rejects.toEqual(new ApiError(403, body));
  });
});

describe('fetchApi signed-out detection (E-1)', () => {
  const caught = (p: Promise<unknown>) => p.then(() => null, (e: unknown) => e);

  it('asks for JSON so Authelia answers 401 instead of redirecting to its login page', async () => {
    await fetchApi('/api/x');
    expect(sentHeaders().get('accept')).toBe('application/json');
  });

  it('maps a fetch that was redirected to another origin to a signed-out 401', async () => {
    const res = new Response('<html>Sign in</html>', { status: 200, headers: { 'content-type': 'text/plain' } });
    Object.defineProperties(res, {
      redirected: { value: true },
      url: { value: 'https://auth.romulus.hu/?rd=https%3A%2F%2Ftrading.romulus.hu%2Fapi%2Ftrading%2Fkill-switch' },
    });
    fetchMock.mockResolvedValueOnce(res);
    const err = await caught(fetchApi('/api/trading/kill-switch', { method: 'POST' }));
    expect(err).toBeInstanceOf(ApiError);
    expect(isSignedOut(err)).toBe(true);
    expect(stateFromError(err)).toBe('unauthenticated');
  });

  it('maps a 2xx HTML page (the login portal) to a signed-out 401', async () => {
    fetchMock.mockResolvedValueOnce(new Response('<!doctype html>', { status: 200, headers: { 'content-type': 'text/html; charset=utf-8' } }));
    expect(isSignedOut(await caught(fetchApi('/api/risk/live-status')))).toBe(true);
  });

  it('keeps a same-origin redirect to JSON as data', async () => {
    const res = new Response('{"ok":true}', { status: 200, headers: { 'content-type': 'application/json' } });
    Object.defineProperties(res, { redirected: { value: true }, url: { value: `${window.location.origin}/api/x/` } });
    fetchMock.mockResolvedValueOnce(res);
    await expect(fetchApi('/api/x')).resolves.toEqual({ ok: true });
  });
});

describe('apiHeaders', () => {
  it('gives raw fetch callers the CSRF header for unsafe methods only', () => {
    expect(apiHeaders()).toEqual({ 'x-hydra-request': '1' });
    expect(apiHeaders('PUT')).toEqual({ 'x-hydra-request': '1' });
    expect(apiHeaders('GET')).toEqual({});
  });
});

describe('connectWebSocket base URL', () => {
  const opened: string[] = [];

  class FakeWebSocket {
    constructor(url: string) {
      opened.push(url);
    }
  }

  async function connectWith(env: Record<string, string>): Promise<string> {
    opened.length = 0;
    for (const [name, value] of Object.entries(env)) vi.stubEnv(name, value);
    vi.stubGlobal('WebSocket', FakeWebSocket);
    vi.resetModules();
    const { connectWebSocket } = await import('./api');
    connectWebSocket('/ws/market', () => {});
    return opened[0];
  }

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('uses NEXT_PUBLIC_WS_URL so REST can stay on the same origin', async () => {
    const url = await connectWith({ NEXT_PUBLIC_WS_URL: 'http://localhost:8000', NEXT_PUBLIC_API_URL: '' });
    expect(url).toBe('ws://localhost:8000/ws/market');
  });

  it('falls back to NEXT_PUBLIC_API_URL, then the page origin', async () => {
    expect(await connectWith({ NEXT_PUBLIC_WS_URL: '', NEXT_PUBLIC_API_URL: 'https://api.example' })).toBe(
      'wss://api.example/ws/market',
    );
    expect(await connectWith({ NEXT_PUBLIC_WS_URL: '', NEXT_PUBLIC_API_URL: '' })).toBe(
      `${window.location.origin.replace(/^http/, 'ws')}/ws/market`,
    );
  });
});
