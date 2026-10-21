import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buttonByText,
  click,
  dialogPolyfill,
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
import { ThemeProvider } from '@/components/layout/ThemeProvider';

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
    },
  };
}

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

beforeEach(() => {
  dialogPolyfill();
  calls = [];
  fetchApi.mockReset();
  fetchApi.mockImplementation(routeFetch(routes(), calls));
});

afterEach(() => {
  page?.unmount();
  page = null;
  vi.unstubAllGlobals();
});

const mountSettings = () =>
  mount(
    <ThemeProvider>
      <SettingsPage />
    </ThemeProvider>,
  );

function puts(): unknown[] {
  return calls
    .filter((c) => c.init?.method === 'PUT')
    .map((c) => JSON.parse(String(c.init?.body)));
}

describe('SettingsPage', () => {
  it('shows the trading mode as a read-only fact, with no control to change it', async () => {
    page = await mountSettings();

    expect(page.container.querySelector('[data-testid="trading-mode"]')?.textContent).toBe(
      'Paper. Live trading arrives with a later release and will need a one-time code to turn on.',
    );
    expect(Array.from(page.container.querySelectorAll('select')).some((s) => s.querySelector('option[value="live"]'))).toBe(false);
  });

  it('says a stored live mode is refused and paper is shown', async () => {
    const r = routes();
    (r['/api/system/config'] as Record<string, unknown>).trading_mode = 'live';
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mountSettings();

    expect(page.container.querySelector('[data-testid="trading-mode"]')?.textContent).toBe(
      'Stored mode is live; live trading is refused until a later release. Showing paper.',
    );
  });

  it('has one h1 and tells the truth about alerts instead of offering toggles and webhook URLs', async () => {
    page = await mountSettings();

    expect(page.container.querySelectorAll('h1')).toHaveLength(1);
    expect(page.container.querySelector('h1')?.textContent).toBe('Settings');
    const text = page.container.textContent ?? '';
    expect(text).toContain('Alerts are sent by Alertmanager to Telegram. Change them in the alert rules, not here.');
    expect(text).toContain("Webhook alerts aren't sent in this version.");
    expect(page.container.querySelector('[role="switch"]')).toBeNull();
    expect(text).not.toContain('Discord');
    expect(text).not.toContain('Kill Switch Active');
    expect(calls.map((c) => c.path)).not.toContain('/api/system/notifications');
    expect(calls.map((c) => c.path)).not.toContain('/api/risk/config');
  });

  it('shows the Grafana link only when NEXT_PUBLIC_GRAFANA_URL is set', async () => {
    page = await mountSettings();
    expect(page.container.querySelector('a[href*="grafana"]')).toBeNull();
    page.unmount();

    vi.stubEnv('NEXT_PUBLIC_GRAFANA_URL', 'https://grafana.example/d/hydra');
    page = await mountSettings();
    expect(page.container.querySelector('a[href="https://grafana.example/d/hydra"]')?.textContent).toBe('Open Grafana');
    vi.unstubAllEnvs();
  });

  it('puts the theme control on Settings, with the stored choice pressed', async () => {
    page = await mountSettings();
    const group = page.container.querySelector('[role="group"][aria-label="Theme"]');
    await click(buttonByText(group as HTMLElement, 'light'));

    expect(buttonByText(group as HTMLElement, 'light').getAttribute('aria-pressed')).toBe('true');
    expect(buttonByText(group as HTMLElement, 'dark').getAttribute('aria-pressed')).toBe('false');
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  it('does not clear the webhook secret when the warning is declined', async () => {
    const ask = vi.fn(() => false);
    vi.stubGlobal('confirm', ask);
    page = await mountSettings();

    await click(buttonByText(page.container, 'Clear'));

    expect(ask).toHaveBeenCalledWith(CLEAR_WEBHOOK_SECRET_WARNING);
    expect(puts()).toEqual([]);
  });

  it('clears the webhook secret once the warning is accepted', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true));
    page = await mountSettings();

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
    environment: 'testnet',
    validated_environment: 'testnet',
  },
  {
    id: 'bybit',
    name: 'Bybit',
    supported: false,
    connected: false,
    api_key_set: true,
    credential_state: 'undecryptable',
    last_sync: null,
    environment: 'testnet',
    validated_environment: null,
  },
  {
    id: 'kraken',
    name: 'Kraken',
    supported: false,
    connected: false,
    api_key_set: false,
    credential_state: 'missing',
    last_sync: null,
    environment: 'testnet',
    validated_environment: null,
  },
];

describe('SettingsPage exchange credential state', () => {
  it('shows when the keys were validated and which stored keys cannot be decrypted', async () => {
    const r = routes();
    r['/api/system/exchanges'] = EXCHANGES;
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mountSettings();

    const binance = page.container.querySelector('[data-testid="validation-binance"]');
    expect(binance?.textContent).toContain('Validated on testnet');
    expect(binance?.textContent).not.toContain('Active');
    expect(page.container.textContent).toContain('Undecryptable, enter the keys again');
    expect(page.container.textContent).toContain('Not supported yet (Binance only)');
    const connect = Array.from(page.container.querySelectorAll('button')).filter(
      (b) => b.textContent?.trim() === 'Connect',
    );
    expect(connect.every((b) => b.disabled)).toBe(true);
  });

  it('does not count keys checked on testnet as validated on mainnet', async () => {
    const r = routes();
    r['/api/system/exchanges'] = [
      { ...EXCHANGES[0], connected: false, environment: 'mainnet', validated_environment: 'testnet' },
    ];
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mountSettings();

    const binance = page.container.querySelector('[data-testid="validation-binance"]');
    expect(binance?.textContent).toBe('Checked on testnet only; connect again for mainnet');
  });

  it('shows the balance when open orders fail and never asks a spot account for positions', async () => {
    const r = routes();
    r['/api/system/exchanges'] = EXCHANGES;
    r['/api/system/exchanges/binance/balance'] = { exchange_id: 'binance', balances: { USDT: 12.5 } };
    r['/api/system/exchanges/binance/orders'] = unavailable('Request to Binance testnet failed');
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mountSettings();
    await click(buttonByText(page.container, 'Details'));

    const balances = page.container.textContent ?? '';
    expect(balances).toContain('USDT');
    expect(balances).toContain('12.50');
    expect(page.container.querySelector('[data-testid="orders-error-binance"]')?.textContent).toBe(
      'Request to Binance testnet failed',
    );
    expect(balances).toContain('Spot account: there are no positions');
    expect(calls.some((c) => c.path.endsWith('/positions'))).toBe(false);
  });

  it('asks before deleting stored keys and deletes only on the dialog confirm', async () => {
    const r = routes();
    r['/api/system/exchanges'] = EXCHANGES;
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mountSettings();

    await click(buttonByText(page.container, 'Delete keys'));
    const dialog = Array.from(page.container.querySelectorAll('dialog')).find((d) =>
      d.textContent?.includes('Delete the stored'),
    ) as HTMLElement;
    expect(dialog.textContent).toContain('Delete the stored Binance keys?');
    expect(document.activeElement?.textContent).toBe('Delete the stored Binance keys?');
    expect(calls.some((c) => c.init?.method === 'DELETE')).toBe(false);

    await click(buttonByText(dialog, 'Delete keys'));
    expect(calls.filter((c) => c.init?.method === 'DELETE').map((c) => c.path)).toEqual([
      '/api/system/exchanges/binance/connect',
    ]);
  });

  it('shows the server message when exchanges cannot be read', async () => {
    const r = routes();
    r['/api/system/exchanges'] = unavailable('Database error while reading exchange credentials');
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mountSettings();

    const card = sectionText(page.container, 'Exchange connections');
    expect(card).toContain('UNAVAILABLE');
    expect(card).toContain('Database error while reading exchange credentials');
  });
});

describe('SettingsPage platform config', () => {
  it('shows why the config is unavailable and no 10000 default', async () => {
    const r = routes();
    r['/api/system/config'] = unavailable('system_config has no row for: paper_capital');
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mountSettings();

    const card = sectionText(page.container, 'Platform configuration');
    expect(card).toContain('system_config has no row for: paper_capital');
    expect(page.container.textContent).not.toContain('10000');
  });

  it('names the ledger reset in a dialog before a paper capital change and sends nothing until confirmed', async () => {
    page = await mountSettings();

    await typeInto(inputByLabel(page.container, 'Paper Trading Capital (USDT)'), '25000');
    await click(buttonByText(page.container, 'Save Configuration'));

    const dialog = page.container.querySelector('dialog');
    expect(dialog?.hasAttribute('open')).toBe(true);
    expect(dialog?.textContent).toContain('Change paper capital to 25000 USDT?');
    expect(dialog?.textContent).toContain('rebases the paper ledger from 10000 to 25000 USDT');
    expect(puts()).toEqual([]);

    await click(buttonByText(page.container, 'Keep current capital'));
    expect(puts()).toEqual([]);
  });

  it('saves the new capital, without the trading mode, once the reset is confirmed', async () => {
    page = await mountSettings();

    await typeInto(inputByLabel(page.container, 'Paper Trading Capital (USDT)'), '25000');
    await click(buttonByText(page.container, 'Save Configuration'));
    await click(buttonByText(page.container, 'Reset ledger to new capital'));

    expect(puts()).toEqual([
      { default_pair: 'BTC/USDT', default_timeframe: '1h', max_concurrent_strategies: 5, paper_capital: 25000 },
    ]);
  });

  it('saves other settings without asking when the capital is unchanged', async () => {
    page = await mountSettings();

    await click(buttonByText(page.container, 'Save Configuration'));

    expect(page.container.querySelector('dialog')?.hasAttribute('open')).toBe(false);
    expect(puts()).toEqual([
      { default_pair: 'BTC/USDT', default_timeframe: '1h', max_concurrent_strategies: 5, paper_capital: 10000 },
    ]);
  });

  it('says a webhook without a secret rejects every signal', () => {
    expect(CLEAR_WEBHOOK_SECRET_WARNING).toContain('rejects every signal');
    expect(CLEAR_WEBHOOK_SECRET_WARNING).not.toContain('accept payloads without a secret');
  });
});
