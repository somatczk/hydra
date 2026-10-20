import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buttonByText, click, mount, routeFetch, type Mounted, type Routes } from '@/test/pageHarness';
import { CLEAR_WEBHOOK_SECRET_WARNING } from '@/lib/platformConfig';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));

const { default: SettingsPage } = await import('./page');

function routes(): Routes {
  return {
    '/api/system/exchanges': [],
    '/api/system/config': {
      trading_mode: 'paper',
      default_pair: 'BTC/USDT',
      default_timeframe: '1h',
      max_concurrent_strategies: 5,
      paper_capital: 10000,
      webhook_secret_set: true,
      discord_webhook_url_set: false,
      slack_webhook_url_set: false,
    },
    '/api/risk/config': { kill_switch_active: false },
    '/api/system/notifications': { preferences: {} },
    '/api/risk/live-status': { kill_switch_active: false },
  };
}

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

beforeEach(() => {
  calls = [];
  fetchApi.mockReset();
  fetchApi.mockImplementation(routeFetch(routes(), calls));
});

afterEach(() => {
  page?.unmount();
  page = null;
  vi.unstubAllGlobals();
});

function puts(): unknown[] {
  return calls
    .filter((c) => c.init?.method === 'PUT')
    .map((c) => JSON.parse(String(c.init?.body)));
}

describe('SettingsPage', () => {
  it('offers only the trading modes the API accepts', async () => {
    page = await mount(<SettingsPage />);
    const select = Array.from(page.container.querySelectorAll('select')).find((s) =>
      Array.from(s.options).some((o) => o.value === 'paper'),
    );
    expect(Array.from(select?.options ?? []).map((o) => o.value)).toEqual(['paper', 'live']);
  });

  it('does not clear the webhook secret when the warning is declined', async () => {
    const ask = vi.fn(() => false);
    vi.stubGlobal('confirm', ask);
    page = await mount(<SettingsPage />);

    await click(buttonByText(page.container, 'Clear'));

    expect(ask).toHaveBeenCalledWith(CLEAR_WEBHOOK_SECRET_WARNING);
    expect(puts()).toEqual([]);
  });

  it('clears the webhook secret once the warning is accepted', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true));
    page = await mount(<SettingsPage />);

    await click(buttonByText(page.container, 'Clear'));

    expect(puts()).toEqual([{ webhook_secret: '' }]);
  });
});
