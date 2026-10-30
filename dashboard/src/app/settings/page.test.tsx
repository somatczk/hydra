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
import { ShellStatusProvider } from '@/components/shell/ShellStatusProvider';
import { liveStatus, perpsStatus } from '@/test/fixtures';

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
    '/api/risk/live-status': liveStatus(),
    '/api/live/perps/status': perpsStatus(),
    '/api/trading/sessions': [],
    '/api/system/config': {
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
      <ShellStatusProvider>
        <SettingsPage />
      </ShellStatusProvider>
    </ThemeProvider>,
  );

function puts(): unknown[] {
  return calls
    .filter((c) => c.init?.method === 'PUT')
    .map((c) => JSON.parse(String(c.init?.body)));
}

describe('SettingsPage live gate', () => {
  it('replaces the read-only trading-mode row with the gate door; the later-release claim is gone', async () => {
    page = await mountSettings();

    const text = page.container.textContent ?? '';
    expect(text).toContain('Turn on live trading…');
    expect(text).toContain('Paper. Every live order will need a one-time code.');
    expect(text).not.toContain('arrives with a later release');
    expect(text).not.toContain('Stored mode is');
    expect(page.container.querySelector('[data-testid="trading-mode"]')).toBeNull();
    expect(calls.map((c) => c.path)).not.toContain('/api/risk/config');
  });

  it('shows the armed state with the turn-off door when the gate is on', async () => {
    const r = routes();
    r['/api/risk/live-status'] = liveStatus({ live_armed: true });
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mountSettings();

    expect(page.container.textContent).toContain('Live trading is on.');
    expect(page.container.textContent).toContain('Turn off live trading…');
    expect(page.container.textContent).not.toContain('Turn on live trading…');
  });

  it('mounts the separate perps gate under the spot gate with its own door and honest off-copy', async () => {
    page = await mountSettings();

    const text = page.container.textContent ?? '';
    expect(text).toContain('Perps trading (USD⌂-M)');
    expect(text).toContain('Turn on perps trading…');
    expect(text).toContain(
      'Off. Perp strategies keep running paper until this leveraged gate is armed — arming live trading never arms perps.',
    );
    // Arming spot never arms perps: the spot row being on leaves the perps door closed.
    const r = routes();
    r['/api/risk/live-status'] = liveStatus({ live_armed: true });
    fetchApi.mockImplementation(routeFetch(r, calls));
    page?.unmount();
    page = await mountSettings();
    const spotOn = page.container.textContent ?? '';
    expect(spotOn).toContain('Live trading is on.');
    expect(spotOn).toContain('Turn on perps trading…');
    expect(spotOn).not.toContain('Perps trading is on');
  });

  it('hides the on-button when the code service is down, and the paper settings keep working', async () => {
    fetchApi.mockImplementation(routeFetch(routes(), calls, { 'POST /api/live/arm': unavailable('approval store unavailable') }));
    page = await mountSettings();

    await click(buttonByText(page.container, 'Turn on live trading…'));
    const dialog = page.container.querySelector('dialog[open]') as HTMLElement;
    await typeInto(inputByLabel(dialog, 'Live capital (USDT)'), '25000');
    await click(buttonByText(dialog, 'Turn on live trading'));
    await click(buttonByText(dialog, 'Cancel'));

    expect(page.container.textContent).toContain('Live actions are hidden until the code service recovers.');
    expect(page.container.textContent).not.toContain('Turn on live trading…');
    expect(sectionText(page.container, 'Platform configuration')).toContain('Paper Trading Capital');
    expect(inputByLabel(page.container, 'Paper Trading Capital (USDT)')).toBeTruthy();
  });
});

describe('SettingsPage', () => {
  it('has one h1 and tells the truth about alerts instead of offering toggles and webhook URLs', async () => {
    page = await mountSettings();

    expect(page.container.querySelectorAll('h1')).toHaveLength(1);
    expect(page.container.querySelector('h1')?.textContent).toBe('Settings');
    const text = page.container.textContent ?? '';
    expect(text).toContain(
      'Risk alerts go to Telegram from the engine itself: every risk event is pushed using the bot token and chat id stored write-only in platform config.',
    );
    expect(text).toContain(
      'Infra paging still rides Alertmanager to Telegram (change it in the alert rules, not here).',
    );
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

    const dialog = page.container.querySelector('dialog[open]');
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

  it('keeps the dialog open and shows the failure when the capital save is rejected', async () => {
    fetchApi.mockImplementation(
      routeFetch(routes(), calls, {
        'PUT /api/system/config': unavailable('risk_reload_failed: saved but not applied'),
      }),
    );
    page = await mountSettings();

    await typeInto(inputByLabel(page.container, 'Paper Trading Capital (USDT)'), '25000');
    await click(buttonByText(page.container, 'Save Configuration'));
    await click(buttonByText(page.container, 'Reset ledger to new capital'));

    const dialog = page.container.querySelector('dialog[open]');
    expect(dialog?.textContent).toContain('Change paper capital');
    expect(dialog?.querySelector('[role="alert"]')?.textContent).toContain('Not saved');
  });

  it.each(['', '0', '-5'])('rejects capital %j before any dialog or request', async (value) => {
    page = await mountSettings();

    await typeInto(inputByLabel(page.container, 'Paper Trading Capital (USDT)'), value);
    await click(buttonByText(page.container, 'Save Configuration'));

    expect(page.container.querySelector('dialog')?.hasAttribute('open')).toBe(false);
    expect(page.container.textContent).toContain('Enter a paper capital above 0');
    expect(puts()).toEqual([]);
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
