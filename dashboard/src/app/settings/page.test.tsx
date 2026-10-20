import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buttonByText,
  click,
  inputByLabel,
  mount,
  routeFetch,
  sectionText,
  typeInto,
  unavailable,
  type Mounted,
  type Routes,
} from '@/test/pageHarness';
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
      paper_capital_pending: null,
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

const EXCHANGES = [
  {
    id: 'binance',
    name: 'Binance',
    supported: true,
    connected: true,
    api_key_set: true,
    credential_state: 'ok',
    last_sync: '2026-10-20T12:00:00+00:00',
  },
  {
    id: 'bybit',
    name: 'Bybit',
    supported: false,
    connected: false,
    api_key_set: true,
    credential_state: 'undecryptable',
    last_sync: null,
  },
  {
    id: 'kraken',
    name: 'Kraken',
    supported: false,
    connected: false,
    api_key_set: false,
    credential_state: 'missing',
    last_sync: null,
  },
];

describe('SettingsPage exchange credential state', () => {
  it('shows when the keys were validated and which stored keys cannot be decrypted', async () => {
    const r = routes();
    r['/api/system/exchanges'] = EXCHANGES;
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mount(<SettingsPage />);

    const binance = page.container.querySelector('[data-testid="validation-binance"]');
    expect(binance?.textContent).toContain('Validated');
    expect(binance?.textContent).not.toContain('Active');
    expect(page.container.textContent).toContain('Undecryptable, enter the keys again');
    expect(page.container.textContent).toContain('Not supported yet (Binance only)');
    const connect = Array.from(page.container.querySelectorAll('button')).filter(
      (b) => b.textContent?.trim() === 'Connect',
    );
    expect(connect.every((b) => b.disabled)).toBe(true);
  });

  it('shows the server message when exchanges cannot be read', async () => {
    const r = routes();
    r['/api/system/exchanges'] = unavailable('Database error while reading exchange credentials');
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mount(<SettingsPage />);

    const card = sectionText(page.container, 'Exchange Connections');
    expect(card).toContain('UNAVAILABLE');
    expect(card).toContain('Database error while reading exchange credentials');
  });
});

describe('SettingsPage platform config', () => {
  it('shows why the config is unavailable and no 10000 default', async () => {
    const r = routes();
    r['/api/system/config'] = unavailable('system_config has no row for: paper_capital');
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mount(<SettingsPage />);

    const card = sectionText(page.container, 'Platform Configuration');
    expect(card).toContain('system_config has no row for: paper_capital');
    expect(page.container.textContent).not.toContain('10000');
  });

  it('asks before a paper capital change, which resets the paper ledger', async () => {
    const ask = vi.fn(() => false);
    vi.stubGlobal('confirm', ask);
    page = await mount(<SettingsPage />);

    await typeInto(inputByLabel(page.container, 'Paper Trading Capital (USDT)'), '25000');
    await click(buttonByText(page.container, 'Save Configuration'));

    expect(String((ask.mock.calls[0] as unknown[])[0])).toContain('from 10000 to 25000');
    expect(puts()).toEqual([]);
  });

  it('saves other settings without asking when the capital is unchanged', async () => {
    const ask = vi.fn(() => false);
    vi.stubGlobal('confirm', ask);
    page = await mount(<SettingsPage />);

    await click(buttonByText(page.container, 'Save Configuration'));

    expect(ask).not.toHaveBeenCalled();
    expect(puts()).toEqual([
      {
        trading_mode: 'paper',
        default_pair: 'BTC/USDT',
        default_timeframe: '1h',
        max_concurrent_strategies: 5,
        paper_capital: 10000,
      },
    ]);
  });

  it('says a webhook without a secret rejects every signal', () => {
    expect(CLEAR_WEBHOOK_SECRET_WARNING).toContain('rejects every signal');
    expect(CLEAR_WEBHOOK_SECRET_WARNING).not.toContain('accept payloads without a secret');
  });
});
