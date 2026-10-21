import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buttonByText, click, dialogPolyfill, mount, routeFetch, type Mounted, type Mutations, type Routes } from '@/test/pageHarness';
import { detail, liveStatus, platformConfig, session } from '@/test/fixtures';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }), usePathname: () => '/trading' }));

const { default: SessionsPage } = await import('./page');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');

const RUN = session({ session_id: 'run-1', strategy_name: 'SMA cross' });
const HOLDER = session({ session_id: 'held-1', strategy_name: 'Momentum 4h', status: 'stopped', stopped_at: '2026-10-21T10:00:00+00:00', flatten_incomplete: true, positions_at_stop: 1 });
const QUIET = session({ session_id: 'quiet-1', strategy_name: 'RSI revert', status: 'stopped', stopped_at: '2026-10-21T09:00:00+00:00' });

let m: Mounted;
let calls: Array<{ path: string; init?: RequestInit }>;

function setup(list: ReturnType<typeof session>[], routes: Routes = {}, mutations: Mutations = {}) {
  fetchApi.mockImplementation(
    routeFetch(
      {
        '/api/risk/live-status': liveStatus(),
        '/api/trading/sessions': list,
        '/api/system/config': platformConfig(),
        '/api/trading/sessions/run-1/detail': detail({
          ...RUN,
          positions: [{ symbol: 'BTCUSDT', direction: 'long', quantity: 0.0015, avg_entry_price: 67000, unrealized_pnl: 5 }],
          metrics: { equity: 5000, realized_pnl: 0, unrealized_pnl: 5, fees: 0, drawdown: 0 },
        }),
        ...routes,
      },
      calls,
      mutations,
    ),
  );
}

const page = () => mount(<ShellStatusProvider><SessionsPage /></ShellStatusProvider>);
const text = () => m.container.textContent ?? '';

beforeEach(() => {
  dialogPolyfill();
  calls = [];
});

afterEach(() => {
  m.unmount();
  fetchApi.mockReset();
});

describe('Sessions list', () => {
  it('says nothing is running when the list is empty', async () => {
    setup([]);
    m = await page();
    expect(text()).toContain('Nothing is running. Start a paper session from a strategy.');
    expect(text()).toContain('Running (0)');
  });

  it('shows a stopped holder outside the collapsed Stopped group', async () => {
    setup([RUN, HOLDER, QUIET]);
    m = await page();
    expect(text()).toContain('Momentum 4h');
    expect(text()).toContain('still holds 1');
    expect(text()).not.toContain('RSI revert');
    await click(buttonByText(m.container, 'Stopped (1)'));
    expect(text()).toContain('RSI revert');
  });

  it('reads health and positions of a running session from its detail', async () => {
    setup([RUN]);
    m = await page();
    expect(text()).toContain('Stop failures none');
    expect(text()).toContain('1 position · +5.00 USDT');
  });

  it('has no page-level kill switch and calls no risk config or funding endpoint', async () => {
    setup([RUN]);
    m = await page();
    expect(text()).not.toMatch(/kill switch|halted/i);
    expect(calls.some((c) => c.path.includes('/api/risk/config') || c.path.includes('funding-rates'))).toBe(false);
  });

  it('shows the server reason and a retry when the list fails', async () => {
    setup([], { '/api/trading/sessions': new Error('boom') });
    m = await page();
    expect(buttonByText(m.container, 'Retry')).toBeTruthy();
  });

  it('confirms a stop from the table row and sends one DELETE for that session', async () => {
    setup([RUN], {}, { 'DELETE /api/trading/sessions/run-1': session({ session_id: 'run-1', status: 'stopped' }) });
    m = await page();
    const row = Array.from(m.container.querySelectorAll('button')).find((b) => b.textContent === 'Stop session…') as HTMLButtonElement;
    await click(row);
    const dialog = m.container.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.textContent).toContain('Stop SMA cross?');
    expect(dialog.textContent).toContain('1 open position');
    await click(buttonByText(dialog, 'Stop and close 1 position'));
    const deletes = calls.filter((c) => c.init?.method === 'DELETE');
    expect(deletes.map((c) => c.path)).toEqual(['/api/trading/sessions/run-1']);
    expect(text()).toContain('All positions closed.');
  });
});
