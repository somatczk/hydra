import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, dialogPolyfill, flush, mount, routeFetch, type Mounted, type Mutations, type Routes } from '@/test/pageHarness';
import { detail, liveStatus, platformConfig, session } from '@/test/fixtures';

vi.mock('next/navigation', () => ({ usePathname: () => '/risk' }));
const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { AppLayout } = await import('@/components/layout/AppLayout');
const { ThemeProvider } = await import('@/components/layout/ThemeProvider');

const RUN_A = session({ session_id: 'run-a', strategy_name: 'SMA cross' });
const RUN_B = session({ session_id: 'run-b', strategy_name: 'RSI revert', timeframe: '15m' });
const HOLDER = session({
  session_id: 'held-1',
  strategy_name: 'Momentum 4h',
  status: 'stopped',
  flatten_incomplete: true,
  positions_at_stop: 1,
});

const NOT_SAVED = new ApiError(
  503,
  JSON.stringify({
    detail: { code: 'kill_switch_not_persisted', message: 'Kill switch is on in memory but could not be saved', state: 'unavailable', sessions_stopped: 0, positions_remaining: 0, flatten_failures: [], persisted: false },
  }),
);

let m: Mounted;
let calls: Array<{ path: string; init?: RequestInit }>;
let ksOn: boolean;
let list: ReturnType<typeof session>[];

function setup(routes: Routes = {}, mutations: Mutations = {}) {
  fetchApi.mockImplementation(
    routeFetch(
      {
        '/api/risk/live-status': () => liveStatus({ kill_switch_active: ksOn, kill_switch_state: ksOn ? 'active' : 'inactive' }),
        '/api/trading/sessions': () => list,
        '/api/system/config': platformConfig(),
        '/api/trading/sessions/run-a/detail': detail({ ...RUN_A, positions: [{ symbol: 'BTCUSDT', direction: 'long', quantity: 0.0015, avg_entry_price: 1, unrealized_pnl: 0 }] }),
        '/api/trading/sessions/run-b/detail': detail({
          ...RUN_B,
          positions: [
            { symbol: 'BTCUSDT', direction: 'long', quantity: 0.001, avg_entry_price: 1, unrealized_pnl: 0 },
            { symbol: 'BTCUSDT', direction: 'long', quantity: 0.002, avg_entry_price: 1, unrealized_pnl: 0 },
          ],
        }),
        ...routes,
      },
      calls,
      mutations,
    ),
  );
}

const shell = () =>
  mount(
    <ThemeProvider>
      <AppLayout>
        <h1>Risk</h1>
      </AppLayout>
    </ThemeProvider>,
  );

const text = () => document.body.textContent ?? '';
const dialog = () => m.container.querySelector('dialog') as HTMLDialogElement;
const posts = () => calls.filter((c) => c.path === '/api/trading/kill-switch' && c.init?.method === 'POST');
const stopAll = () => m.container.querySelector<HTMLButtonElement>('button[aria-label="Stop all trading"]') as HTMLButtonElement;

async function confirmStop() {
  await click(stopAll());
  const primary = Array.from(dialog().querySelectorAll('button')).find((b) => b.textContent?.startsWith('Stop all trading'));
  await click(primary as HTMLButtonElement);
  await flush();
}

async function refetch() {
  await act(async () => {
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await flush();
}

beforeEach(() => {
  localStorage.clear();
  dialogPolyfill();
  calls = [];
  ksOn = false;
  list = [RUN_A, RUN_B, HOLDER];
});

afterEach(() => {
  m.unmount();
  fetchApi.mockReset();
});

describe('KillSwitchControl', () => {
  it('offers only Stop all when the state is unknown, and no release anywhere in the shell', async () => {
    setup({ '/api/risk/live-status': liveStatus({ kill_switch_active: null, kill_switch_state: 'unknown' }) });
    m = await shell();
    expect(buttonByText(m.container, 'Stop all').getAttribute('aria-label')).toBe('Stop all trading');
    expect(text()).toContain('Kill switch state unknown');
    expect(text()).toContain('UNKNOWN');
    expect(text()).not.toMatch(/release/i);
  });

  it('shows the ON chip linking to Risk when the kill switch is on', async () => {
    ksOn = true;
    setup();
    m = await shell();
    const chip = Array.from(m.container.querySelectorAll('a')).find((a) => a.textContent?.startsWith('Kill switch ON'));
    expect(chip?.getAttribute('href')).toBe('/risk#kill-switch');
    expect(chip?.className).toContain('max-lg:h-11');
    expect(stopAll()).toBeNull();
  });

  it('opens a confirm focused on its title that counts targets and names stopped holders', async () => {
    setup();
    m = await shell();
    const trigger = stopAll();
    trigger.focus();
    await click(trigger);
    expect(dialog().open).toBe(true);
    expect(document.activeElement?.textContent).toBe('Stop all trading?');
    const body = dialog().textContent ?? '';
    expect(body).toContain('This stops 2 running sessions and closes 3 open positions at market (paper).');
    expect(body).toContain("Still held in 1 stopped session: Momentum 4h. The kill switch won't close these");
    expect(buttonByText(dialog(), 'Stop all trading and close 3 positions')).toBeTruthy();

    expect(document.activeElement?.tagName).not.toBe('BUTTON');

    await act(async () => {
      dialog().dispatchEvent(new Event('cancel', { cancelable: true }));
    });
    expect(dialog().open).toBe(false);
    expect(document.activeElement).toBe(trigger);
  });

  it('closes the sheet when the switch turns on elsewhere, and does not reopen it when it turns off', async () => {
    setup();
    m = await shell();
    await click(stopAll());
    expect(dialog().open).toBe(true);
    ksOn = true;
    await refetch();
    expect(text()).toContain('Kill switch ON');
    ksOn = false;
    await refetch();
    expect(stopAll()).not.toBeNull();
    expect(m.container.querySelector('dialog[open]')).toBeNull();
    expect(posts()).toHaveLength(0);
  });

  it('keeps the primary enabled while positions are still being counted', async () => {
    setup({ '/api/trading/sessions/run-a/detail': () => new Promise(() => {}) });
    m = await shell();
    await click(stopAll());
    expect(dialog().textContent).toContain('Open positions: counting…');
    expect(buttonByText(dialog(), 'Stop all trading').disabled).toBe(false);
  });

  it('posts once on a double click, then reads a fresh session list (R-P0-1 case 2)', async () => {
    let resolvePost: (v: unknown) => void = () => {};
    setup({}, {
      'POST /api/trading/kill-switch': () =>
        new Promise((r) => {
          resolvePost = r;
        }),
    });
    m = await shell();
    await click(stopAll());
    const primary = buttonByText(dialog(), 'Stop all trading and close 3 positions');
    await act(async () => {
      primary.click();
      primary.click();
    });
    expect(posts()).toHaveLength(1);
    expect(dialog().textContent).toContain('Stopping…');
    expect(buttonByText(dialog(), 'Close')).toBeTruthy();
    expect(dialog().textContent).toContain("Closing this won't cancel the stop.");

    const postAt = calls.length;
    ksOn = true;
    list = [{ ...RUN_A, status: 'stopped' }, { ...RUN_B, status: 'stopped' }, HOLDER];
    await act(async () => resolvePost({ active: true, message: 'ok', sessions_stopped: 0, positions_remaining: 0, persisted: true, flatten_failures: [] }));
    await flush();

    expect(calls.slice(postAt).some((c) => c.path === '/api/trading/sessions' && !c.init?.method)).toBe(true);
    const outcome = m.container.querySelector('[aria-label="Outcome"]')?.textContent ?? '';
    expect(outcome).toContain('Kill switch on.');
    expect(outcome).toContain('Still held in 1 stopped session: Momentum 4h.');
    expect(outcome).not.toContain('No positions are held');
    expect(outcome).not.toContain('All positions closed');
    expect(m.container.querySelector('dialog[open]')).toBeNull();
    expect(document.activeElement?.textContent).toContain('Kill switch ON');
  });

  it('tries closing each holder with DELETE and never posts the kill switch again', async () => {
    const second = session({ session_id: 'held-2', strategy_name: 'Breakout', status: 'error', flatten_incomplete: true });
    setup({}, {
      'POST /api/trading/kill-switch': { active: true, message: 'ok', sessions_stopped: 2, positions_remaining: 0, persisted: true, flatten_failures: [] },
      'DELETE /api/trading/sessions/held-1': { ...HOLDER, flatten_incomplete: false, positions_at_stop: 0 },
      'DELETE /api/trading/sessions/held-2': { ...second, flatten_incomplete: false, positions_at_stop: 0 },
    });
    m = await shell();
    ksOn = true;
    list = [HOLDER, second];
    await confirmStop();
    expect(text()).toContain('Still held in 2 stopped sessions: Momentum 4h, Breakout.');

    await click(buttonByText(m.container, 'Try closing again'));
    const deletes = calls.filter((c) => c.init?.method === 'DELETE').map((c) => c.path);
    expect(deletes).toEqual(['/api/trading/sessions/held-1', '/api/trading/sessions/held-2']);
    expect(posts()).toHaveLength(1);
    const outcome = m.container.querySelector('[aria-label="Outcome"]')?.textContent ?? '';
    expect(outcome).toContain('Tried closing 2 sessions again.');
    expect(outcome).toContain('Momentum 4h: Stopped at');
  });

  it('renders C-KS-4 on a 503, flags Needs you, and Save again renders C-KS-4-SAVED without a positions claim', async () => {
    const notSaved = new ApiError(
      503,
      JSON.stringify({
        detail: {
          code: 'kill_switch_not_persisted',
          message: 'Kill switch is on in memory but could not be saved',
          state: 'unavailable',
          sessions_stopped: 2,
          positions_remaining: 0,
          flatten_failures: [],
          persisted: false,
        },
      }),
    );
    setup({}, {
      'POST /api/trading/kill-switch': (n: number) =>
        n === 1 ? notSaved : { active: true, message: 'ok', sessions_stopped: 0, positions_remaining: 0, persisted: true, flatten_failures: [] },
    });
    list = [RUN_A, RUN_B];
    m = await shell();
    ksOn = true;
    list = [{ ...RUN_A, status: 'stopped' }, { ...RUN_B, status: 'stopped' }];
    await confirmStop();
    const alert = () => m.container.querySelector('[aria-label="Outcome alert"]')?.textContent ?? '';
    expect(alert()).toContain('Kill switch on, but not saved.');
    expect(alert()).toContain('No positions are held');
    expect(text()).toContain('Needs you: 1');

    await click(buttonByText(m.container, 'Save again'));
    expect(posts()).toHaveLength(2);
    const outcome = m.container.querySelector('[aria-label="Outcome"]')?.textContent ?? '';
    expect(outcome).toContain('Kill switch saved.');
    expect(outcome).not.toMatch(/position/i);
    expect(alert()).toBe('');
    expect(text()).toContain('Nothing needs you');
  });

  it('keeps the not-saved flag across a reload until live-status reads off', async () => {
    setup({}, {
      'POST /api/trading/kill-switch': NOT_SAVED,
    });
    list = [];
    m = await shell();
    ksOn = true;
    await confirmStop();
    expect(text()).toContain('Needs you: 1');

    m.unmount();
    m = await shell();
    await flush();
    expect(text()).toContain('Needs you: 1');

    ksOn = false;
    await refetch();
    expect(text()).toContain('Nothing needs you');
    expect(localStorage.getItem('hydra-ks-not-saved')).toBeNull();
  });

  it('retries a failed Save again as Save again, not as a new Stop all', async () => {
    const saved = { active: true, message: 'ok', sessions_stopped: 0, positions_remaining: 0, persisted: true, flatten_failures: [] };
    setup({}, {
      'POST /api/trading/kill-switch': (n: number) => (n === 1 ? NOT_SAVED : n === 2 ? new TypeError('Failed to fetch') : saved),
    });
    list = [];
    m = await shell();
    ksOn = true;
    await confirmStop();
    await click(buttonByText(m.container, 'Save again'));
    expect(text()).toContain("we don't know whether the kill switch was saved");

    const before = calls.length;
    await click(buttonByText(m.container, 'Try again'));
    await flush();
    expect(posts()).toHaveLength(3);
    expect(calls.slice(before).filter((c) => c.path === '/api/trading/sessions')).toHaveLength(1);
    const outcome = m.container.querySelector('[aria-label="Outcome"]')?.textContent ?? '';
    expect(outcome).toContain('Kill switch saved.');
    expect(outcome).not.toMatch(/position/i);
  });

  it('says signed out, not unconfirmed, when the POST meets the sign-in wall (C-KS-7)', async () => {
    setup({}, { 'POST /api/trading/kill-switch': new ApiError(401, 'Unauthorized') });
    m = await shell();
    await confirmStop();
    expect(text()).toContain("Not sent: you're signed out.");
    expect(text()).not.toContain('Not confirmed');
  });

  it('says not confirmed on a network failure (C-KS-5)', async () => {
    setup({}, { 'POST /api/trading/kill-switch': new TypeError('Failed to fetch') });
    m = await shell();
    await confirmStop();
    expect(text()).toContain('Not confirmed.');
    expect(text()).toContain("we don't know whether trading stopped");
  });
});
