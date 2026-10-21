import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buttonByText, click, inputByLabel, mount, routeFetch, typeInto, type Mounted } from '@/test/pageHarness';
import { detail } from '@/test/fixtures';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { QuickTrade } = await import('./QuickTrade');

const HELD = [{ symbol: 'BTCUSDT', direction: 'long', quantity: 0.002, avg_entry_price: 60000, unrealized_pnl: 10 }];
let m: Mounted;
let calls: Array<{ path: string; init?: RequestInit }>;

function show(positions = HELD) {
  fetchApi.mockImplementation(
    routeFetch({ '/api/strategies/sma-cross-btc-1h': { risk: { stop_loss_method: 'atr', stop_loss_value: 2 } } }, calls, {
      'POST /api/trading/quick-trade': { order_id: 'abcdef123456', session_id: detail().session_id, symbol: 'BTCUSDT', side: 'buy', order_type: 'MARKET', quantity: 0.0015, price: null, status: 'FILLED', protection: 'armed', protective_order_id: 'p1', exit_orders_deferred: false, fill_price: 67612.4, mark_source: 'live' },
    }),
  );
  return mount(<QuickTrade session={detail()} positions={positions} onOutcome={vi.fn()} onSent={vi.fn()} />);
}

const primary = () => m.container.querySelector<HTMLButtonElement>('button[type="submit"]') as HTMLButtonElement;
const side = (label: string) => buttonByText(m.container, label);
const radio = (label: string) =>
  Array.from(m.container.querySelectorAll('label')).find((l) => l.textContent === label)?.querySelector('input') as HTMLInputElement;

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  m.unmount();
  fetchApi.mockReset();
});

describe('QuickTrade', () => {
  it('restates side, quantity and paper in the primary and the live summary', async () => {
    m = await show();
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.0015');
    expect(primary().textContent).toBe('Buy 0.0015 BTC (paper)');
    expect(m.container.textContent).toContain('Buy 0.0015 BTC at market (paper).');
    expect(m.container.textContent).toContain('Strategy default stop (2.0 × ATR, set at fill)');
    expect(m.container.textContent).toContain('About 97.50 USDT');
    expect(primary().disabled).toBe(false);
  });

  it('disables the primary when the stop is not below the take-profit', async () => {
    m = await show();
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.001');
    await click(radio('Stop price'));
    await typeInto(inputByLabel(m.container, 'Stop price (USDT)'), '70000');
    await typeInto(inputByLabel(m.container, 'Take-profit price (optional)'), '65000');
    expect(m.container.textContent).toContain('Stop price must be below take-profit price.');
    expect(primary().disabled).toBe(true);
  });

  it('cannot combine a trailing stop with a stop price', async () => {
    m = await show();
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.001');
    await click(radio('Stop price'));
    await typeInto(inputByLabel(m.container, 'Stop price (USDT)'), '60000');
    await click(radio('Trailing stop (%)'));
    await typeInto(inputByLabel(m.container, 'Trailing distance (%)'), '2');
    expect(Array.from(m.container.querySelectorAll('label')).some((l) => l.textContent === 'Stop price (USDT)')).toBe(false);
    await click(primary());
    const body = JSON.parse(calls.find((c) => c.path === '/api/trading/quick-trade')?.init?.body as string);
    expect(body.trailing_stop_pct).toBe('2');
    expect(body.stop_loss).toBeUndefined();
  });

  it('rejects trailing distances in exponent form or beyond 8 decimals', async () => {
    m = await show();
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.001');
    await click(radio('Trailing stop (%)'));
    for (const bad of ['1e1', '5.123456789']) {
      await typeInto(inputByLabel(m.container, 'Trailing distance (%)'), bad);
      expect(primary().disabled).toBe(true);
    }
    await typeInto(inputByLabel(m.container, 'Trailing distance (%)'), '5.12345678');
    expect(primary().disabled).toBe(false);
  });

  it('refuses a sell when nothing is held', async () => {
    m = await show([]);
    await click(side('Sell'));
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.001');
    expect(m.container.textContent).toContain("Nothing to sell. Spot can't go short.");
    expect(primary().disabled).toBe(true);
  });

  it('asks for a limit price and sends it as a string', async () => {
    m = await show();
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.001');
    await click(side('Limit'));
    expect(m.container.textContent).toContain('A limit order needs a price.');
    expect(primary().disabled).toBe(true);
    await typeInto(inputByLabel(m.container, 'Limit price'), '65000.5');
    await click(primary());
    const body = JSON.parse(calls.find((c) => c.path === '/api/trading/quick-trade')?.init?.body as string);
    expect(body).toMatchObject({ order_type: 'LIMIT', price: '65000.5', quantity: '0.001' });
  });
});
