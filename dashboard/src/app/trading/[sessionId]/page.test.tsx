import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, dialogPolyfill, flush, inputByLabel, mount, routeFetch, typeInto, type Mounted, type Mutations, type Routes } from '@/test/pageHarness';
import { detail, liveStatus, paperFill, platformConfig, session } from '@/test/fixtures';

const ID = 'a1b2c3d4-0000-4000-8000-000000000001';
const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('next/navigation', () => ({ useParams: () => ({ sessionId: ID }), useRouter: () => ({ push: vi.fn() }), usePathname: () => `/trading/${ID}` }));
vi.mock('@/components/layout/ThemeProvider', () => ({ useTheme: () => ({ resolved: 'light' }) }));

const { default: SessionDetailPage } = await import('./page');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');

const HELD = { symbol: 'BTCUSDT', direction: 'long', quantity: 0.0015, avg_entry_price: 67612.4, unrealized_pnl: -1.42 };
const BOOK = { symbol: 'BTCUSDT', bids: [[65000.5, 1.25]], asks: [[65001, 0.75]], spread: 0.5, timestamp: null, fetched_at: new Date().toISOString() };

let m: Mounted;
let calls: Array<{ path: string; init?: RequestInit }>;

function setup(d: ReturnType<typeof detail>, mutations: Mutations = {}, routes: Routes = {}) {
  fetchApi.mockImplementation(
    routeFetch(
      {
        [`/api/trading/sessions/${ID}/detail`]: d,
        '/api/risk/live-status': liveStatus(),
        '/api/trading/sessions': [session()],
        '/api/system/config': platformConfig(),
        '/api/market/orderbook?symbol=BTCUSDT': BOOK,
        '/api/strategies/sma-cross-btc-1h': { risk: { stop_loss_method: 'fixed_pct', stop_loss_value: 5 } },
        ...routes,
      },
      calls,
      mutations,
    ),
  );
}

const page = () => mount(<ShellStatusProvider><SessionDetailPage /></ShellStatusProvider>);
const text = () => m.container.textContent ?? '';
const dialog = () => m.container.querySelector('dialog') as HTMLDialogElement;

beforeEach(() => {
  dialogPolyfill();
  calls = [];
  vi.stubGlobal('WebSocket', class { onopen = null; onclose = null; onerror = null; onmessage = null; close() {} });
});

afterEach(() => {
  m.unmount();
  fetchApi.mockReset();
  vi.unstubAllGlobals();
});

describe('session detail', () => {
  it('reads fill quantity from amount and marks a fill with neither as unknown', async () => {
    setup(detail({ trades: [paperFill(), paperFill({ id: 'ord-2', amount: undefined, quantity: 0 })] }));
    m = await page();
    expect(text()).toContain('0.00150');
    expect(text()).toContain('quantity not reported');
    expect(text()).toContain('UNKNOWN');
  });

  it('never claims a stop is armed, shows the fill Reason and has no Mark column', async () => {
    setup(detail({ positions: [HELD], trades: [paperFill({ reason: 'signal' })] }));
    m = await page();
    const headers = Array.from(m.container.querySelectorAll('th')).map((h) => h.textContent);
    expect(text()).not.toMatch(/stop armed/i);
    expect(headers).toContain('Reason');
    expect(text()).toContain('signal');
    expect(headers.some((h) => /mark/i.test(h ?? ''))).toBe(false);
    expect(headers).toContain('Avg entry');
  });

  it('states stop failures and the stop price from the protective order', async () => {
    setup(
      detail({
        protection_failures: ['x'],
        open_orders: [{ id: 'o1', symbol: 'BTCUSDT', side: 'SELL', type: 'STOP', amount: 0.0015, price: null, stop_price: 64256, reason: 'protective stop', protective: true }],
      }),
    );
    m = await page();
    expect(text()).toContain('1 stop failure since start. Last: x');
    expect(text()).toContain('Stop at 64,256.00 (BTCUSDT)');
    expect(text()).toContain('Polling every 15 s');
  });

  it('shows unsaved ledger changes and a dead feed from the shell session row', async () => {
    setup(detail(), {}, { '/api/trading/sessions': [session({ persist_dirty: true, feed_live: false })] });
    m = await page();
    expect(text()).toContain('Session status saved: yes · Unsaved ledger changes');
    expect(text()).toContain('Feed not live');
  });

  it('shows no feed warning and a clean save when the shell row is healthy', async () => {
    setup(detail(), {}, { '/api/trading/sessions': [session({ persist_dirty: false, feed_live: true })] });
    m = await page();
    expect(text()).not.toContain('Feed not live');
    expect(text()).not.toContain('Unsaved ledger changes');
  });

  it('sends one DELETE for a double click on Try closing again', async () => {
    setup(detail({ status: 'stopped', positions: [], positions_at_stop: 1, flatten_incomplete: true }), {
      [`DELETE /api/trading/sessions/${ID}`]: session({ status: 'stopped', positions_at_stop: 1, flatten_incomplete: true }),
    });
    m = await page();
    const btn = buttonByText(m.container, 'Try closing again');
    btn.click();
    btn.click();
    await flush();
    expect(calls.filter((c) => c.init?.method === 'DELETE')).toHaveLength(1);
  });

  it('says a stopped session still holds when its position list is empty', async () => {
    setup(detail({ status: 'stopped', positions: [], positions_at_stop: 1, flatten_incomplete: true }));
    m = await page();
    expect(text()).toContain("1 position still held; details aren't available for a stopped session");
    expect(text()).not.toContain('No open positions');
  });

  it('hides quick trade unless the session is running', async () => {
    setup(detail({ status: 'stopped' }));
    m = await page();
    expect(text()).not.toContain('Trade in this session');
    expect(text()).not.toContain('Stop session…');
  });

  it('names the positions at stake in the stop confirm and sends one DELETE', async () => {
    setup(detail({ positions: [HELD] }), { [`DELETE /api/trading/sessions/${ID}`]: session({ status: 'stopped' }) });
    m = await page();
    await click(buttonByText(m.container, 'Stop session…'));
    expect(dialog().textContent).toContain('1 open position');
    expect(dialog().textContent).toContain('0.00150 BTC');
    await click(buttonByText(dialog(), 'Stop and close 1 position'));
    expect(calls.filter((c) => c.init?.method === 'DELETE')).toHaveLength(1);
    expect(text()).toMatch(/Stopped at .*UTC\./);
    expect(text()).toContain('All positions closed.');
  });

  it('sends the quantity as a string and shows the risk rejection verbatim', async () => {
    const rejected = new ApiError(
      422,
      JSON.stringify({ detail: { code: 'risk_rejected', message: 'Position would exceed the heat cap', check: 'heat_cap', state: 'invalid' } }),
    );
    setup(detail(), { 'POST /api/trading/quick-trade': rejected });
    m = await page();
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.0015');
    await click(buttonByText(m.container, 'Buy 0.0015 BTC (paper)'));
    const post = calls.find((c) => c.path === '/api/trading/quick-trade');
    const body = JSON.parse(post?.init?.body as string);
    expect(body).toMatchObject({ quantity: '0.0015', side: 'buy', order_type: 'MARKET', session_id: ID, symbol: 'BTCUSDT' });
    expect(body.stop_loss).toBeUndefined();
    expect(text()).toContain('Not placed: Position would exceed the heat cap (heat_cap).');
  });
});
