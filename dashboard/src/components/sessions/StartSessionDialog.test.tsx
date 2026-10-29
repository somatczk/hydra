import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, dialogPolyfill, inputByLabel, mount, routeFetch, typeInto, unavailable, type Mounted, type Mutations } from '@/test/pageHarness';
import { liveChallenge, liveStatus, platformConfig, session } from '@/test/fixtures';

const fetchApi = vi.fn();
const push = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push }) }));

const { StartSessionDialog } = await import('./StartSessionDialog');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');

let m: Mounted;
let calls: Array<{ path: string; init?: RequestInit }>;

function show(live: unknown = liveStatus(), mutations: Mutations = {}) {
  fetchApi.mockImplementation(
    routeFetch(
      {
        '/api/risk/live-status': live,
        '/api/trading/sessions': [],
        '/api/system/config': platformConfig(),
        '/api/strategies/sma-cross-btc-1h': { id: 'sma-cross-btc-1h', name: 'SMA cross', symbol: 'BTCUSDT', timeframes: { primary: '1h' } },
      },
      calls,
      mutations,
    ),
  );
  return mount(<ShellStatusProvider><StartSessionDialog strategyId="sma-cross-btc-1h" onClose={vi.fn()} /></ShellStatusProvider>);
}

const primary = () => buttonByText(m.container, 'Start paper session');
const text = () => m.container.textContent ?? '';

beforeEach(() => {
  dialogPolyfill();
  calls = [];
  push.mockReset();
});

afterEach(() => {
  m.unmount();
  fetchApi.mockReset();
});

describe('StartSessionDialog', () => {
  it('names what starts and sends the capital as a string, then opens the session', async () => {
    m = await show(liveStatus(), { 'POST /api/trading/sessions': session({ session_id: 'new-1', paper_capital: 6000 }) });
    expect(text()).toContain('SMA cross · Binance spot · BTCUSDT · 1h · PAPER');
    await typeInto(inputByLabel(m.container, 'Paper capital for this session (USDT)'), '8000');
    await click(primary());
    const body = JSON.parse(calls.find((c) => c.init?.method === 'POST')?.init?.body as string);
    expect(body).toEqual({ strategy_id: 'sma-cross-btc-1h', trading_mode: 'paper', paper_capital: '8000' });
    expect(push).toHaveBeenCalledWith('/trading/new-1');
  });

  it('says the kill switch state is unknown when live status cannot be read', async () => {
    m = await show(unavailable());
    expect(text()).toContain('Kill switch state unknown. The server will refuse the start if it is on.');
  });

  it('disables the primary and points to Risk while the kill switch is on', async () => {
    m = await show(liveStatus({ kill_switch_active: true, kill_switch_state: 'active' }));
    expect(primary().disabled).toBe(true);
    expect(text()).toContain('The kill switch is on. Release it on Risk before starting a session.');
    expect(m.container.querySelector('a[href="/risk#kill-switch"]')).not.toBeNull();
  });

  it('says nothing was started when the engine refuses', async () => {
    const refused = new ApiError(
      409,
      JSON.stringify({ detail: { code: 'session_already_running', message: 'Already running', state: 'invalid' } }),
    );
    m = await show(liveStatus(), { 'POST /api/trading/sessions': refused });
    await click(primary());
    expect(text()).toContain('This strategy already has a running session.');
    expect(push).not.toHaveBeenCalled();
  });

  it('rejects a capital with more than 8 decimals before sending', async () => {
    m = await show();
    await typeInto(inputByLabel(m.container, 'Paper capital for this session (USDT)'), '1.123456789');
    expect(text()).toContain('Enter an amount above 0 with up to 8 decimals.');
    expect(primary().disabled).toBe(true);
  });
});

describe('StartSessionDialog live door', () => {
  it('offers Live disabled with the Settings pointer while the gate is off', async () => {
    m = await show();
    const liveRadio = buttonByText(m.container, 'live');
    expect(liveRadio.disabled).toBe(true);
    expect(liveRadio.getAttribute('aria-pressed')).toBe('false');
    expect(text()).toContain('Live needs the gate on — turn on live trading in Settings first.');
    expect(text()).toContain('SMA cross · Binance spot · BTCUSDT · 1h · PAPER');
  });
  it('starts a live session through the code-gated ceremony and navigates to it', async () => {
    const CHALLENGE = liveChallenge({ approval_id: '3f0b9e2a-3000-4000-8000-000000000009', code: '482913' });
    m = await show(liveStatus({ live_armed: true }), {
      'POST /api/live/session-start': CHALLENGE,
      'POST /api/activity/approvals/3f0b9e2a-3000-4000-8000-000000000009/approve': {
        executed: true,
        result: session({ session_id: 'new-live', trading_mode: 'live', started_at: '2026-10-21T08:00:00+00:00' }),
        actor: 'user:soma',
        approved_by: 'user:soma',
      },
    });
    await click(buttonByText(m.container, 'live'));
    expect(text()).toContain('SMA cross · Binance spot · BTCUSDT · 1h · LIVE');
    expect(text()).toContain(
      "Live sessions place real orders on Binance with real money. Paper sessions keep running. Every live order you place needs the one-time code; once a live session starts, its strategy trades under that start's authorization — bounded by the kill switch, risk limits and exchange stops.",
    );
    const go = buttonByText(m.container, 'Start live session');
    await typeInto(inputByLabel(m.container, 'Live capital for this session (USDT)'), '8000');
    await click(go);
    const startCall = calls.find((c) => c.path === '/api/live/session-start');
    expect(JSON.parse(startCall?.init?.body as string)).toEqual({
      strategy_id: 'sma-cross-btc-1h',
      paper_capital: '8000',
      reason: 'Live session: SMA cross',
    });
    expect(text()).toContain('482913');
    expect(buttonByText(m.container, 'Start live session').disabled).toBe(true);
    await typeInto(inputByLabel(m.container, 'One-time code'), '482913');
    await click(buttonByText(m.container, 'Start live session'));
    expect(
      calls.filter((c) => c.path === '/api/activity/approvals/3f0b9e2a-3000-4000-8000-000000000009/approve').map((c) => JSON.parse(c.init?.body as string)),
    ).toEqual([{ code: '482913' }]);
    expect(push).toHaveBeenCalledWith('/trading/new-live');
  });

  it('sends the paper start unchanged while the gate is armed but Paper is picked', async () => {
    m = await show(liveStatus({ live_armed: true }), { 'POST /api/trading/sessions': session({ session_id: 'new-1' }) });
    await click(buttonByText(m.container, 'Start paper session'));
    const body = JSON.parse(calls.find((c) => c.init?.method === 'POST')?.init?.body as string);
    expect(body).toEqual({ strategy_id: 'sma-cross-btc-1h', trading_mode: 'paper' });
    expect(calls.some((c) => c.path === '/api/live/session-start')).toBe(false);
  });

  it('renders a 503 engine_unreachable approve as not-confirmed with the Open Sessions link (R30(1))', async () => {
    const CHALLENGE = liveChallenge({ approval_id: '3f0b9e2a-3000-4000-8000-000000000009', code: '482913' });
    const timedOut = new ApiError(
      503,
      JSON.stringify({
        detail: {
          code: 'engine_unreachable',
          message:
            'The trading engine did not answer within 20s — the operation may still be executing; check the session or order state before re-submitting',
          state: 'unavailable',
        },
      }),
    );
    m = await show(liveStatus({ live_armed: true }), {
      'POST /api/live/session-start': CHALLENGE,
      'POST /api/activity/approvals/3f0b9e2a-3000-4000-8000-000000000009/approve': timedOut,
    });
    await click(buttonByText(m.container, 'live'));
    await click(buttonByText(m.container, 'Start live session'));
    await typeInto(inputByLabel(m.container, 'One-time code'), '482913');
    await click(buttonByText(m.container, 'Start live session'));
    expect(text()).toContain('Not confirmed.');
    expect(text()).toContain(
      "We couldn't reach the engine, so we don't know whether the live session started. Check the sessions list before trying again.",
    );
    expect(text()).toContain('the operation may still be executing');
    expect(m.container.querySelector('a[href="/trading"]')?.textContent).toBe('Open Sessions');
    expect(text()).not.toContain('Nothing was started.');
    expect(push).not.toHaveBeenCalled();
  });

  it('resets to the form after a definite refused live start, for a fresh challenge', async () => {
    const CHALLENGE = liveChallenge({ approval_id: '3f0b9e2a-3000-4000-8000-000000000009', code: '482913' });
    const APPROVE = 'POST /api/activity/approvals/3f0b9e2a-3000-4000-8000-000000000009/approve';
    m = await show(liveStatus({ live_armed: true }), {
      'POST /api/live/session-start': CHALLENGE,
      [APPROVE]: { executed: false, result: null, actor: 'user:soma', approved_by: 'user:soma' },
    });
    await click(buttonByText(m.container, 'live'));
    await click(buttonByText(m.container, 'Start live session'));
    expect(text()).toContain('482913');
    await typeInto(inputByLabel(m.container, 'One-time code'), '482913');
    await click(buttonByText(m.container, 'Start live session'));
    expect(text()).toContain('Nothing was started.');
    expect(Array.from(m.container.querySelectorAll('label')).map((l) => l.textContent)).not.toContain('One-time code');
    expect(inputByLabel(m.container, 'Live capital for this session (USDT)').disabled).toBe(false);
    await click(buttonByText(m.container, 'Start live session'));
    expect(calls.filter((c) => c.path === '/api/live/session-start')).toHaveLength(2);
    expect(calls.filter((c) => c.path === '/api/activity/approvals/3f0b9e2a-3000-4000-8000-000000000009/approve')).toHaveLength(1);
    expect(text()).toContain('482913');
  });
});

describe('StartSessionDialog perp door (P4-E: a perp strategy answers to the perps gate)', () => {
  const PERP_DETAIL = {
    id: 'perp-trend',
    name: 'Perp trend',
    symbol: 'BTCUSDT',
    timeframes: { primary: '1h' },
    exchange_id: 'binanceusdm',
  };
  const PERPS_FACTS_LINE =
    "up to 3× on isolated margin, so each position's loss is capped at the margin it posted and a liquidation takes that margin to zero.";

  function showPerp(live: unknown, mutations: Mutations = {}) {
    fetchApi.mockImplementation(
      routeFetch(
        {
          '/api/risk/live-status': live,
          '/api/trading/sessions': [],
          '/api/system/config': platformConfig(),
          '/api/strategies/perp-trend': PERP_DETAIL,
        },
        calls,
        mutations,
      ),
    );
    return mount(<ShellStatusProvider><StartSessionDialog strategyId="perp-trend" onClose={vi.fn()} /></ShellStatusProvider>);
  }

  it('names the perps venue on the strategy line', async () => {
    m = await showPerp(liveStatus());
    expect(text()).toContain('Perp trend · Binance USD⌂-M perps · BTCUSDT · 1h · PAPER');
    expect(text()).not.toContain('Binance spot');
  });

  it('keeps Live disabled while only the SPOT gate is armed — arming spot never arms perps', async () => {
    m = await showPerp(liveStatus({ live_armed: true, perps_armed: false }));
    const liveRadio = buttonByText(m.container, 'live');
    expect(liveRadio.disabled).toBe(true);
    expect(text()).toContain('Live needs the perps gate on — turn on perps trading in Settings first.');
  });

  it('offers Live once the PERPS gate is armed, with the leveraged facts and the perps capital hint', async () => {
    m = await showPerp(liveStatus({ perps_armed: true }));
    expect(buttonByText(m.container, 'live').disabled).toBe(false);
    await click(buttonByText(m.container, 'live'));
    expect(text()).toContain('Perp trend · Binance USD⌂-M perps · BTCUSDT · 1h · LIVE');
    expect(text()).toContain(PERPS_FACTS_LINE);
    const capital = inputByLabel(m.container, 'Live capital for this session (USDT)');
    expect(capital.parentElement?.textContent).toContain('Leave empty to use all unallocated perps capital.');
  });

  it('starts a live perp session through the same code-gated session-start ceremony', async () => {
    const CHALLENGE = liveChallenge({ approval_id: '3f0b9e2a-3000-4000-8000-000000000010', code: '482913' });
    m = await showPerp(liveStatus({ perps_armed: true }), {
      'POST /api/live/session-start': CHALLENGE,
      'POST /api/activity/approvals/3f0b9e2a-3000-4000-8000-000000000010/approve': {
        executed: true,
        result: session({ session_id: 'new-perp', trading_mode: 'live', exchange_id: 'binanceusdm' }),
        actor: 'user:soma',
        approved_by: 'user:soma',
      },
    });
    await click(buttonByText(m.container, 'live'));
    await click(buttonByText(m.container, 'Start live session'));
    const startCall = calls.find((c) => c.path === '/api/live/session-start');
    expect(JSON.parse(startCall?.init?.body as string)).toEqual({
      strategy_id: 'perp-trend',
      reason: 'Live session: Perp trend',
    });
    expect(text()).toContain('482913');
    await typeInto(inputByLabel(m.container, 'One-time code'), '482913');
    await click(buttonByText(m.container, 'Start live session'));
    expect(push).toHaveBeenCalledWith('/trading/new-perp');
  });
});
