import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, dialogPolyfill, inputByLabel, mount, routeFetch, typeInto, type Mounted, type Mutations } from '@/test/pageHarness';
import { liveStatus, platformConfig, session } from '@/test/fixtures';

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

function show(live = liveStatus(), mutations: Mutations = {}) {
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
