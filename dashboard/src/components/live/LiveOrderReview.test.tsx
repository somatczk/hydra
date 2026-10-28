import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { act } from 'react';
import { ApiError } from '@/lib/api';
import { buttonByText, click, inputByLabel, mount, routeFetch, typeInto, unavailable, type Mounted, type Mutations } from '@/test/pageHarness';
import { detail, liveChallenge } from '@/test/fixtures';
import type { QuickTradeRequest } from '@/lib/outcomes';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { LiveOrderReview } = await import('./LiveOrderReview');

const APPROVE_PATH = '/api/activity/approvals/3f0b9e2a-2000-4000-8000-000000000001/approve';
const R19 =
  "Live sessions place real orders on Binance with real money. Paper sessions keep running. Every live order you place needs the one-time code; once a live session starts, its strategy trades under that start's authorization — bounded by the kill switch, risk limits and exchange stops.";
const REQ: QuickTradeRequest = { symbol: 'BTCUSDT', side: 'buy', order_type: 'MARKET', quantity: '0.0015', session_id: detail().session_id };
const ABOUT = "About 97.50 USDT at the last closed bar's price.";
const FILLED = {
  order_id: 'abcdef123456',
  session_id: detail().session_id,
  symbol: 'BTCUSDT',
  side: 'buy',
  order_type: 'MARKET',
  quantity: 0.0015,
  price: null,
  status: 'FILLED',
  protection: 'armed',
  protective_order_id: 'p1',
  exit_orders_deferred: false,
  fill_price: 67612.4,
  mark_source: 'live',
};

const envelope = (status: number, code: string, message: string, state: string, extra: Record<string, unknown> = {}) =>
  new ApiError(status, JSON.stringify({ detail: { code, message, state, ...extra } }));

let m: Mounted;
let calls: Array<{ path: string; init?: RequestInit }>;
let placed: Mock;
let sent: Mock;
let outcome: Mock;

function show(mutations: Mutations = {}) {
  placed = vi.fn();
  sent = vi.fn();
  outcome = vi.fn();
  fetchApi.mockImplementation(
    routeFetch({}, calls, {
      'POST /api/live/order': liveChallenge({ note: 'Enter this code once to place the live order.' }),
      ...mutations,
    }),
  );
  return mount(
    <LiveOrderReview
      session={detail({ trading_mode: 'live' })}
      req={REQ}
      about={ABOUT}
      onOutcome={outcome}
      onSent={sent}
      onPlaced={placed}
      onBack={vi.fn()}
    />,
  );
}

const primary = () => buttonByText(m.container, 'Place live order');
const codeField = () => inputByLabel(m.container, 'One-time code');
const typeAmount = (v: string) => typeInto(inputByLabel(m.container, 'Type 0.0015 to confirm'), v);
const submitForm = async () => {
  const form = m.container.querySelector('form');
  expect(form).not.toBeNull();
  await act(async () => {
    form?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
};

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  m.unmount();
  fetchApi.mockReset();
});

describe('LiveOrderReview', () => {
  it('states the live facts, the exact order and the session', async () => {
    m = await show();
    expect(m.container.textContent).toContain(R19);
    expect(m.container.textContent).toContain(`Buy 0.0015 BTC with real money on Binance · ${ABOUT} · Strategy default stop on the exchange.`);
    expect(m.container.textContent).toContain('Session SMA cross.');
  });

  it('creates the challenge on open and shows the engine code once beside the field', async () => {
    m = await show();
    const body = JSON.parse(calls.find((c) => c.path === '/api/live/order')?.init?.body as string);
    expect(body).toMatchObject({ ...REQ, reason: 'Manual live order: buy 0.0015 BTC in SMA cross' });
    expect(m.container.textContent).toContain('482913');
    expect(codeField().autocomplete).toBe('one-time-code');
    expect(codeField().inputMode).toBe('numeric');
  });

  it('sends nothing until the typed amount matches exactly and the code is 6 digits', async () => {
    m = await show();
    expect(primary().disabled).toBe(true);
    await typeAmount('0.0015');
    expect(primary().disabled).toBe(true);
    await typeInto(codeField(), '48291');
    expect(primary().disabled).toBe(true);
    await typeInto(codeField(), '482913');
    expect(primary().disabled).toBe(false);
    await click(primary());
    expect(JSON.parse(calls.find((c) => c.path === APPROVE_PATH)?.init?.body as string)).toEqual({ code: '482913' });
  });

  it('does not submit on Enter at open', async () => {
    m = await show();
    await submitForm();
    expect(calls.filter((c) => c.path === APPROVE_PATH)).toHaveLength(0);
  });

  it('a wrong code stays inline, focuses the code field and keeps the values', async () => {
    m = await show({
      [`POST ${APPROVE_PATH}`]: envelope(422, 'code_mismatch', "That code didn't match. Nothing changed.", 'invalid', { attempts_left: 4 }),
    });
    await typeAmount('0.0015');
    await typeInto(codeField(), '000000');
    await click(primary());
    expect(m.container.textContent).toContain("That code didn't match. Nothing was placed.");
    expect(m.container.textContent).toContain('4 attempts left.');
    expect(codeField().value).toBe('000000');
    expect(inputByLabel(m.container, 'Type 0.0015 to confirm').value).toBe('0.0015');
    expect(document.activeElement).toBe(codeField());
  });

  it('a filled order says real money, clears the form and refetches', async () => {
    m = await show({
      [`POST ${APPROVE_PATH}`]: { executed: true, result: FILLED, actor: 'user:soma', approved_by: 'user:soma' },
    });
    await typeAmount('0.0015');
    await typeInto(codeField(), '482913');
    await click(primary());
    expect(placed).toHaveBeenCalledTimes(1);
    expect(sent).toHaveBeenCalledTimes(1);
    expect(outcome).toHaveBeenCalledTimes(1);
    expect((outcome.mock.calls[0] as [{ title: string }])[0].title).toBe('Bought 0.00150 BTC with real money.');
  });

  it('an executor failure ends the challenge — this screen can never place a second order', async () => {
    m = await show({
      [`POST ${APPROVE_PATH}`]: envelope(503, 'approval_execution_failed', "The approved action 'live_order' did not run: exchange unreachable", 'unavailable'),
    });
    await typeAmount('0.0015');
    await typeInto(codeField(), '482913');
    await click(primary());
    expect(m.container.textContent).toContain('Not confirmed.');
    expect(m.container.textContent).toContain("We don't know whether the order was placed. Check this session's fills and positions before trying again.");
    expect(() => primary()).toThrow();
    expect(buttonByText(m.container, 'Back')).toBeTruthy();
  });

  it('an unavailable code service hides the primary and says no order was placed', async () => {
    fetchApi.mockImplementation(routeFetch({}, calls, { 'POST /api/live/order': unavailable() }));
    m = await mount(
      <LiveOrderReview
        session={detail({ trading_mode: 'live' })}
        req={REQ}
        about={ABOUT}
        onOutcome={vi.fn()}
        onSent={vi.fn()}
        onPlaced={vi.fn()}
        onBack={vi.fn()}
      />,
    );
    expect(m.container.textContent).toContain('No order was placed.');
    expect(m.container.textContent).toContain('Live actions are hidden until the code service recovers.');
    expect(() => primary()).toThrow();
    expect(buttonByText(m.container, 'Back')).toBeTruthy();
  });
});
