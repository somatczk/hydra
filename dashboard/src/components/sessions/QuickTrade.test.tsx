import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buttonByText, click, inputByLabel, mount, routeFetch, typeInto, type Mounted, type Mutations } from '@/test/pageHarness';
import { detail, liveChallenge, perpSession } from '@/test/fixtures';
import type { QuickTradeResponse } from '@/lib/outcomes';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { QuickTrade } = await import('./QuickTrade');

const HELD = [{ symbol: 'BTCUSDT', direction: 'long', quantity: 0.002, avg_entry_price: 60000, unrealized_pnl: 10 }];
const SHORT = [{ symbol: 'BTCUSDT', direction: 'short', quantity: 0.002, avg_entry_price: 60000, unrealized_pnl: 10 }];
let m: Mounted;
let calls: Array<{ path: string; init?: RequestInit }>;

const FILLED: QuickTradeResponse = { order_id: 'abcdef123456', session_id: detail().session_id, symbol: 'BTCUSDT', side: 'buy', order_type: 'MARKET', quantity: 0.0015, price: null, status: 'FILLED', protection: 'armed', protective_order_id: 'p1', exit_orders_deferred: false, fill_price: 67612.4, mark_source: 'live' };

function show(session = detail(), positions = HELD, mutations: Mutations = {}) {
  fetchApi.mockImplementation(
    routeFetch({ '/api/strategies/sma-cross-btc-1h': { risk: { stop_loss_method: 'atr', stop_loss_value: 2 } } }, calls, {
      'POST /api/trading/quick-trade': FILLED,
      ...mutations,
    }),
  );
  return mount(<QuickTrade session={session} positions={positions} onOutcome={vi.fn()} onSent={vi.fn()} />);
}

/** A perp (binanceusdm) paper session — the §4(2) long-only surface. */
function showPerp(positions = HELD, mutations: Mutations = {}) {
  return show(detail({ ...perpSession(), symbols: ['BTCUSDT'] }), positions, mutations);
}

/** The L4 world: a running live session; the form's primary opens the review, never the quick-trade POST. */
function showLive(mutations: Mutations = {}) {
  fetchApi.mockImplementation(
    routeFetch({ '/api/strategies/sma-cross-btc-1h': { risk: { stop_loss_method: 'atr', stop_loss_value: 2 } } }, calls, {
      'POST /api/live/order': liveChallenge({ note: 'Enter this code once to place the live order.' }),
      ...mutations,
    }),
  );
  return mount(<QuickTrade session={detail({ trading_mode: 'live' })} positions={HELD} onOutcome={vi.fn()} onSent={vi.fn()} />);
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
    m = await show(detail(), []);
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

  it('live: the summary reads (live), the primary opens the review and no order POST leaves the form', async () => {
    m = await showLive();
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.0015');
    expect(m.container.textContent).toContain('Buy 0.0015 BTC at market (live).');
    expect(primary().textContent).toBe('Review live order');
    await click(primary());
    expect(calls.filter((c) => c.path === '/api/trading/quick-trade')).toHaveLength(0);
    expect(calls.filter((c) => c.path === '/api/live/order')).toHaveLength(1);
    expect(m.container.textContent).toContain('Place live order');
    expect(m.container.textContent).toContain('482913');
  });

  it('live: the challenge carries the exact typed values and the session', async () => {
    m = await showLive();
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.0015');
    await click(primary());
    const body = JSON.parse(calls.find((c) => c.path === '/api/live/order')?.init?.body as string);
    expect(body).toMatchObject({
      symbol: 'BTCUSDT',
      side: 'buy',
      order_type: 'MARKET',
      quantity: '0.0015',
      session_id: detail().session_id,
      reason: 'Manual live order: buy 0.0015 BTC in SMA cross',
    });
  });

  it('live: Back returns to the form with the values kept', async () => {
    m = await showLive();
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.0015');
    await click(primary());
    await click(buttonByText(m.container, 'Back'));
    expect(inputByLabel(m.container, 'Quantity (BTC)').value).toBe('0.0015');
    expect(primary().textContent).toBe('Review live order');
  });

  it('paper: still POSTs directly from the form with today’s body (regression pin)', async () => {
    m = await show();
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.0015');
    expect(primary().textContent).toBe('Buy 0.0015 BTC (paper)');
    await click(primary());
    expect(calls.filter((c) => c.path === '/api/trading/quick-trade')).toHaveLength(1);
    expect(calls.filter((c) => c.path === '/api/live/order')).toHaveLength(0);
  });
});

describe('QuickTrade on perps (§4(2): manual orders are long-only in 2.8.0)', () => {
  const cut = 'long-only in 2.8.0';

  it('visibly disables the Sell side with no long held and names the cut — never half-broken', async () => {
    m = await showPerp([]);
    const sell = side('Sell');
    expect(sell.disabled).toBe(true);
    expect(m.container.textContent).toContain(`No BTC long held: manual orders on perpetual sessions are ${cut} (use a strategy session to trade shorts).`);
    // Long-only means exactly that: the BUY stays available and posts.
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.001');
    expect(primary().disabled).toBe(false);
    await click(primary());
    expect(calls.filter((c) => c.path === '/api/trading/quick-trade')).toHaveLength(1);
  });

  it('keeps the Sell side enabled when a long is held — reducing it is the safety net, not a short', async () => {
    m = await showPerp(HELD);
    expect(side('Sell').disabled).toBe(false);
    await click(side('Sell'));
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.001');
    expect(primary().disabled).toBe(false);
    expect(m.container.textContent).not.toContain(cut);
  });

  it('refuses the manual BUY over a strategy-held short: the strategy closes its own (BUY-back)', async () => {
    m = await showPerp(SHORT);
    expect(side('Buy').disabled).toBe(true);
    expect(m.container.textContent).toContain(`BTCUSDT is held short by the strategy: manual orders on perpetual sessions are ${cut} — its own exit rules and the kill switch close it (BUY-back).`);
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.001');
    expect(primary().disabled).toBe(true);
    await click(primary());
    expect(calls.filter((c) => c.path === '/api/trading/quick-trade')).toHaveLength(0);
  });

  it('keeps the spot rules byte-identical: Sell with nothing held is the spot refusal, not the perp cut', async () => {
    m = await show(detail(), []);
    expect(side('Sell').disabled).toBe(false);
    await click(side('Sell'));
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.001');
    expect(m.container.textContent).toContain("Nothing to sell. Spot can't go short.");
    expect(m.container.textContent).not.toContain(cut);
  });

  it('the live perp review names the leveraged venue, not plain Binance spot', async () => {
    m = await show(detail({ ...perpSession(), trading_mode: 'live', symbols: ['BTCUSDT'] }), HELD, {
      'POST /api/live/order': liveChallenge({ note: 'Enter this code once to place the live order.' }),
    });
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.0015');
    expect(primary().textContent).toBe('Review live order');
    await click(primary());
    expect(m.container.textContent).toContain('Buy 0.0015 BTC with real leveraged money on Binance USD⌂-M perps');
    expect(m.container.textContent).not.toContain('with real money on Binance');
  });
});
