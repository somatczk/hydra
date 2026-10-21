import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { advance, dialogPolyfill, routeFetch, unavailable } from './pageHarness';

describe('routeFetch mutations', () => {
  it('scripts a 503 then a 200 for the same POST, and lets a GET change after it', async () => {
    let sessions: unknown[] = [{ status: 'running' }];
    const calls: Array<{ path: string; init?: RequestInit }> = [];
    const f = routeFetch({ '/api/trading/sessions': () => sessions }, calls, {
      'POST /api/trading/kill-switch': (n: number) => (n === 1 ? unavailable('not persisted') : { active: true }),
    });
    await expect(f('/api/trading/kill-switch', { method: 'POST' })).rejects.toBeInstanceOf(ApiError);
    sessions = [{ status: 'stopped' }];
    await expect(f('/api/trading/kill-switch', { method: 'POST' })).resolves.toEqual({ active: true });
    await expect(f('/api/trading/sessions')).resolves.toEqual([{ status: 'stopped' }]);
    await expect(f('/api/other', { method: 'DELETE' })).resolves.toEqual({});
    expect(calls.map((c) => `${c.init?.method ?? 'GET'} ${c.path}`)).toEqual([
      'POST /api/trading/kill-switch',
      'POST /api/trading/kill-switch',
      'GET /api/trading/sessions',
      'DELETE /api/other',
    ]);
  });
});

describe('dialogPolyfill', () => {
  it('gives jsdom a modal dialog that opens, closes once and fires close', () => {
    dialogPolyfill();
    const d = document.createElement('dialog');
    const onClose = vi.fn();
    d.addEventListener('close', onClose);
    d.showModal();
    expect(d.open).toBe(true);
    d.close('done');
    d.close();
    expect(d.open).toBe(false);
    expect(d.returnValue).toBe('done');
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('advance', () => {
  it('runs due timers', async () => {
    vi.useFakeTimers();
    const tick = vi.fn();
    setTimeout(tick, 10_000);
    await advance(9_999);
    expect(tick).not.toHaveBeenCalled();
    await advance(1);
    expect(tick).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
