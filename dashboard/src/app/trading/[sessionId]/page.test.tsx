import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, dialogPolyfill, flush, inputByLabel, mount, routeFetch, typeInto, type Mounted, type Mutations, type Routes } from '@/test/pageHarness';
import { detail, exchangeStop, liveChallenge, liveStatus, paperFill, perpHedgedPositions, perpPair, perpSession, perpTrailingStop, perpsState, platformConfig, reconcile, session } from '@/test/fixtures';
import type { OpenOrder } from '@/lib/derive';

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

  it('shows the LIVE chip by the h1 and the real-money account note for a live session', async () => {
    setup(detail({ trading_mode: 'live' }));
    m = await page();
    const heading = m.container.querySelector('h1')?.parentElement?.textContent ?? '';
    expect(heading).toContain('LIVE');
    expect(heading).toContain('real money');
    const account = document.getElementById('account-heading')?.parentElement?.textContent ?? '';
    expect(account).toContain('Real money on Binance.');
  });

  it('shows neither the LIVE chip nor the real-money note on a paper session', async () => {
    setup(detail());
    m = await page();
    expect(m.container.querySelector('h1')?.parentElement?.textContent ?? '').not.toContain('LIVE');
    expect(text()).not.toContain('Real money on Binance');
  });

  it('names the venue a live bybit session runs on and charts it under the BYBIT prefix', async () => {
    setup(detail({ trading_mode: 'live', exchange_id: 'bybit' }));
    m = await page();
    const account = document.getElementById('account-heading')?.parentElement?.textContent ?? '';
    expect(account).toContain('Real money on Bybit.');
    const src = m.container.querySelector('iframe')?.getAttribute('src') ?? '';
    expect(decodeURIComponent(src)).toContain('symbol=BYBIT:BTCUSDT');
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

  it('shows Claude on the header when the MCP actor started the session, the raw principal otherwise, and nothing when unknown', async () => {
    setup(detail({ created_by: 'service:mcp' }));
    m = await page();
    const header = m.container.querySelector('header')?.textContent ?? '';
    expect(header).toContain('Claude');
    expect(header).not.toContain('service:mcp');
    m.unmount();

    setup(detail({ created_by: 'user:soma' }));
    m = await page();
    expect(m.container.querySelector('header')?.textContent).toContain('user:soma');
    m.unmount();

    setup(detail({ created_by: null }));
    m = await page();
    const h = m.container.querySelector('header')?.textContent ?? '';
    expect(h).not.toContain('Claude');
    expect(h).not.toContain('user:soma');
  });
});

describe('session detail: the live reads (UI-4c)', () => {
  const LIVE_POS = { ...HELD, exchange_stop: exchangeStop() };
  const MIN = 60_000;
  /** The page ages rows against the shell clock, so the fixture is relative to the real now. */
  const reconcileDd = () => {
    const dt = Array.from(m.container.querySelectorAll('dt')).find((n) => n.textContent === 'Reconcile');
    return dt?.nextElementSibling?.textContent ?? '';
  };

  it('shows the reconcile row on a live session in each A7 state, and none on paper', async () => {
    setup(detail({ trading_mode: 'live', reconcile: reconcile({ last_reconciled_at: new Date(Date.now() - 5 * MIN).toISOString() }) }));
    m = await page();
    expect(reconcileDd()).toContain('Reconciled 5 min ago');
    m.unmount();

    setup(detail({ trading_mode: 'live', reconcile: reconcile({ state: 'mismatch', detail: 'untracked order 123 on BTC/USDT (not managed)' }) }));
    m = await page();
    expect(reconcileDd()).toContain('Mismatch: untracked order 123 on BTC/USDT (not managed)');
    m.unmount();

    setup(detail({ trading_mode: 'live', reconcile: reconcile({ state: 'never', last_reconciled_at: null }) }));
    m = await page();
    expect(reconcileDd()).toContain('Not reconciled yet');
    m.unmount();

    setup(detail({ trading_mode: 'live', reconcile: reconcile({ state: 'unknown' }) }));
    m = await page();
    expect(reconcileDd()).toContain('UNKNOWN');
    m.unmount();

    setup(detail({ trading_mode: 'live', reconcile: null }));
    m = await page();
    expect(reconcileDd()).toBe('');
    m.unmount();

    setup(detail());
    m = await page();
    expect(reconcileDd()).toBe('');
  });

  it('reads each held position’s exchange stop: confirmed, missing, unknown', async () => {
    setup(detail({ trading_mode: 'live', positions: [LIVE_POS] }));
    m = await page();
    expect(Array.from(m.container.querySelectorAll('th')).map((h) => h.textContent)).toContain('Exchange stop');
    expect(text()).toContain('Stop at 64,256.00 on the exchange');
    expect(text()).not.toContain('No exchange stop');
    m.unmount();

    setup(detail({ trading_mode: 'live', positions: [{ ...HELD, exchange_stop: exchangeStop({ state: 'missing', kind: null, stop_price: null, take_profit: null, order_list_id: null }) }] }));
    m = await page();
    expect(text()).toContain('No exchange stop for BTCUSDT');
    m.unmount();

    setup(detail({ trading_mode: 'live', positions: [{ ...HELD, exchange_stop: exchangeStop({ state: 'unknown' }) }] }));
    m = await page();
    const cells = Array.from(m.container.querySelectorAll('tbody td')).map((td) => td.textContent ?? '');
    expect(cells.some((c) => c.includes('UNKNOWN'))).toBe(true);
    expect(text()).not.toContain('No exchange stop for BTCUSDT');
  });

  it('an unreadable verify names the server error it reported, on both A8 surfaces (S2)', async () => {
    setup(detail({ trading_mode: 'live', positions: [{ ...HELD, exchange_stop: exchangeStop({ state: 'unknown', error: 'Binance unreachable during the verify' }) }] }));
    m = await page();
    const stops = Array.from(m.container.querySelectorAll('dt')).find((n) => n.textContent === 'Stops')?.nextElementSibling?.textContent ?? '';
    expect(stops).toContain('Binance unreachable during the verify');
    expect(m.container.querySelector('tbody [data-state="unknown"]')?.getAttribute('title')).toBe('Binance unreachable during the verify');
  });

  it('never shows the exchange-stop cell or its danger for a paper position (the live-only gate)', async () => {
    setup(detail({ positions: [HELD] }));
    m = await page();
    const headers = Array.from(m.container.querySelectorAll('th')).map((h) => h.textContent);
    expect(headers).not.toContain('Exchange stop');
    expect(text()).not.toContain('No exchange stop');
  });

  it('labels a live fill no reconcile pass has covered, and never a paper fill', async () => {
    setup(detail({ trading_mode: 'live', trades: [paperFill({ reconciled: false }), paperFill({ id: 'ord-2', reconciled: true })] }));
    m = await page();
    const rows = Array.from(m.container.querySelectorAll('tbody tr')).map((r) => r.textContent ?? '');
    expect(rows.filter((r) => r.includes('not reconciled'))).toHaveLength(1);
    m.unmount();

    setup(detail({ trades: [paperFill()] }));
    m = await page();
    expect(text()).not.toContain('not reconciled');
  });

  it('QuickTrade in a live session opens the review and posts no order from the form', async () => {
    setup(detail({ trading_mode: 'live' }), {
      'POST /api/live/order': liveChallenge({ note: 'Enter this code once to place the live order.' }),
    });
    m = await page();
    await typeInto(inputByLabel(m.container, 'Quantity (BTC)'), '0.0015');
    expect(text()).toContain('Buy 0.0015 BTC at market (live).');
    await click(buttonByText(m.container, 'Review live order'));
    expect(calls.filter((c) => c.path === '/api/trading/quick-trade')).toHaveLength(0);
    expect(text()).toContain('Buy 0.0015 BTC with real money on Binance');
    expect(text()).toContain('Place live order');
  });
});

describe('session detail perps surfaces (P4)', () => {
  it('renders the perps block: leverage, isolated margin, the margin wallet and the funding accrual', async () => {
    setup(detail({ ...perpSession(), perps: perpsState() }));
    m = await page();
    const perps = document.getElementById('perps-heading')?.parentElement?.textContent ?? '';
    expect(perps).toContain('2× isolated');
    expect(perps).toContain('400.50 available · 99.50 margin used');
    expect(perps).toContain('Funding applied at 3 settlements.');
    expect(perps).not.toContain('skipped');
    expect(perps).not.toContain('Liquidations');
  });

  it('names the funding skips and a liquidation honestly', async () => {
    setup(detail({ ...perpSession(), perps: perpsState({ funding_applied: 2, funding_missing: 1, liquidations: 1 }) }));
    m = await page();
    const perps = document.getElementById('perps-heading')?.parentElement?.textContent ?? '';
    expect(perps).toContain('Funding applied at 2 settlements; 1 skipped (no stored rate) — P&L shows the gaps honestly.');
    expect(perps).toContain('1 position liquidated at its liquidation price — the wallet got nothing back.');
  });

  it('shows no perps section on a spot session', async () => {
    setup(detail());
    m = await page();
    expect(text()).not.toContain('Margin wallet');
    expect(document.getElementById('perps-heading')).toBeNull();
  });

  it('shows a short position signed, with the BUY-back note; longs stay unsigned', async () => {
    setup(detail({ ...perpSession(), positions: [{ ...HELD, direction: 'short' }] }));
    m = await page();
    const rows = Array.from(m.container.querySelectorAll('tbody tr')).map((r) => r.textContent ?? '');
    expect(rows.some((r) => r.includes('SHORT'))).toBe(true);
    expect(rows.some((r) => r.includes('-0.00150'))).toBe(true);
    expect(text()).toContain("A short closes by buying it back. Its strategy's exit rules and the kill switch close it; manual orders are long-only in 2.8.0.");
  });

  it('keeps the spot positions table byte-identical: Held side, unsigned qty, no BUY-back note', async () => {
    setup(detail({ positions: [HELD] }));
    m = await page();
    const rows = Array.from(m.container.querySelectorAll('tbody tr')).map((r) => r.textContent ?? '');
    expect(rows.some((r) => r.includes('Held'))).toBe(true);
    expect(rows.some((r) => r.includes('-0.00150'))).toBe(false);
    expect(text()).not.toContain('closes by buying it back');
    expect(text()).not.toContain('LONG');
  });
});

describe('session detail hedge-mode surfaces (P7-G)', () => {
  it('renders LONG/SHORT chips with per-side summaries for a hedged perp session', async () => {
    setup(detail({ ...perpSession(), symbols: ['BTCUSDT', 'ETHUSDT'], positions: perpHedgedPositions(), perps: perpsState() }));
    m = await page();
    const chips = Array.from(m.container.querySelectorAll('tbody span')).map((s) => s.textContent);
    expect(chips).toContain('LONG');
    expect(chips).toContain('SHORT');
    expect(text()).toContain('1 position · 0.00200 BTC · unrealised +10.00 USDT');
    expect(text()).toContain('1 position · 0.00150 ETH · unrealised −2.50 USDT');
    expect(text()).toContain("A short closes by buying it back. Its strategy's exit rules and the kill switch close it; manual orders are long-only in 2.8.0.");
  });

  it('renders the resting protective pair as ONE two-leg card with both prices named', async () => {
    setup(detail({ ...perpSession(), perps: perpsState(), open_orders: perpPair() }));
    m = await page();
    const perps = document.getElementById('perps-heading')?.parentElement?.textContent ?? '';
    expect(perps).toContain('Protective pair — one two-leg unit, no OCO on futures');
    expect(perps).toContain('Take-profit 71,000.00 · Stop 64,000.00 · 0.00200 BTC (SELL)');
    expect(perps).toContain('reduce-only — one leg fills and the other cancels');
    expect((perps.match(/Protective pair/g) ?? []).length).toBe(1);
    // The Health row no longer double-lists the pair's legs as two unrelated stops.
    expect(text()).not.toContain('Stop at 71,000.00');
    expect(text()).not.toContain('Stop at 64,000.00 (');
  });

  it('renders a trailing protective group as its trail note', async () => {
    setup(detail({ ...perpSession(), perps: perpsState(), open_orders: [perpTrailingStop()] }));
    m = await page();
    expect(text()).toContain('Trailing stop 2% · stop at 64,800.00 · 0.00200 BTC (SELL)');
  });

  it('names the one-way, isolated and leverage mode line in the perps block', async () => {
    setup(detail({ ...perpSession(), perps: perpsState() }));
    m = await page();
    const perps = document.getElementById('perps-heading')?.parentElement?.textContent ?? '';
    expect(perps).toContain("One-way position mode · Isolated margin · the strategy's leverage 2× (1-3×)");
  });

  it("keeps a spot session's resting OCO in the Health Stops row — no pair card", async () => {
    const oco: OpenOrder[] = [
      { id: 'oco-tp', symbol: 'BTCUSDT', side: 'SELL', type: 'LIMIT', amount: 0.0015, price: 71000, stop_price: null, reason: 'take_profit', protective: true, oco_id: 'oco-1' },
      { id: 'oco-sl', symbol: 'BTCUSDT', side: 'SELL', type: 'STOP_MARKET', amount: 0.0015, price: null, stop_price: 64256, reason: 'stop_loss', protective: true, oco_id: 'oco-1' },
    ];
    setup(detail({ open_orders: oco }));
    m = await page();
    expect(text()).toContain('Stop at 64,256.00 (BTCUSDT)');
    expect(text()).not.toContain('Protective pair');
    expect(text()).not.toContain('Take-profit');
  });
});
