import { act } from 'react';
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

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
const toast = vi.fn();
vi.mock('@/components/ui/Toast', () => ({
  useToast: () => ({ toast: (...args: unknown[]) => toast(...args) }),
}));
vi.mock('@/components/layout/ThemeProvider', () => ({ useTheme: () => ({ resolved: 'light' }) }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));

const { default: TradingPage } = await import('./page');

const BOOK = {
  symbol: 'BTCUSDT',
  exchange: 'binance',
  bids: [[65000.5, 1.25]],
  asks: [[65001, 0.75]],
  spread: 0.5,
  fetched_at: new Date().toISOString(),
};

function baseRoutes(): Routes {
  return {
    '/api/system/config': { trading_mode: 'paper' },
    '/api/portfolio/positions?source=paper': [],
    '/api/portfolio/trades?source=paper': [],
    '/api/trading/sessions': [],
    '/api/risk/config': { kill_switch_active: false },
    '/api/risk/circuit-breakers': unavailable('Circuit breakers are not wired to the engine'),
    '/api/risk/status?source=paper': {
      current_drawdown: 1,
      max_drawdown_limit: 15,
      daily_loss: 40,
      daily_loss_limit: 300,
      circuit_breakers: null,
    },
    '/api/market/orderbook?symbol=BTCUSDT': { ...BOOK, timestamp: null },
    '/api/market/funding-rates/current?symbol=BTCUSDT': [],
  };
}

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

beforeEach(() => {
  calls = [];
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('TradingPage outage states', () => {
  it('shows the Daily PnL vs Limit row as UNAVAILABLE on a risk-status 503', async () => {
    const routes = baseRoutes();
    routes['/api/risk/status?source=paper'] = unavailable();
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TradingPage />);

    const row = sectionText(page.container, 'Daily PnL vs Limit');
    expect(row).toContain('UNAVAILABLE');
    expect(row).toContain('Database is not available');
    expect(row).not.toContain('Loss:');
  });

  it('shows the Daily PnL vs Limit numbers when risk status loads', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<TradingPage />);

    const row = sectionText(page.container, 'Daily PnL vs Limit');
    expect(row).toContain('Loss: $40.00 / Limit: $300.00');
    expect(row).toContain('13%');
  });

  it('shows the Running Sessions card as UNAVAILABLE on a sessions 503', async () => {
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = unavailable();
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TradingPage />);

    const card = sectionText(page.container, 'Running Sessions');
    expect(card).toContain('UNAVAILABLE');
    expect(card).toContain('Database is not available');
  });
});

describe('TradingPage order book timestamps', () => {
  it('does not mark a book without an exchange timestamp as STALE', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<TradingPage />);

    const card = sectionText(page.container, 'Order Book');
    expect(card).toContain('65,000.50');
    expect(card).not.toContain('STALE');
    expect(card).not.toContain('Exchange time');
  });

  it('marks STALE by age only when the exchange gave a timestamp', async () => {
    const routes = baseRoutes();
    routes['/api/market/orderbook?symbol=BTCUSDT'] = {
      ...BOOK,
      timestamp: new Date(Date.now() - 60_000).toISOString(),
    };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TradingPage />);

    const card = sectionText(page.container, 'Order Book');
    expect(card).toContain('STALE');
    expect(card).toContain('Exchange time');
  });

  it('labels the server fetch time honestly after a failed poll', async () => {
    vi.useFakeTimers();
    const routes = baseRoutes();
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TradingPage />);

    routes['/api/market/orderbook?symbol=BTCUSDT'] = unavailable('Order book from binance is unavailable');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });

    const card = sectionText(page.container, 'Order Book');
    expect(card).toContain('STALE');
    expect(card).toContain('Fetched');
    expect(card).not.toContain('Exchange time');
    expect(card).toContain('65,000.50');
  });
});

const SESSIONS = [
  {
    session_id: 'sess-paper',
    strategy_id: 'momentum',
    strategy_name: 'Momentum BTC',
    trading_mode: 'paper',
    status: 'running',
    symbols: ['BTCUSDT'],
    timeframe: '1h',
    started_at: null,
  },
  {
    session_id: 'sess-live',
    strategy_id: 'live-one',
    strategy_name: 'Live One',
    trading_mode: 'live',
    status: 'running',
    symbols: ['BTCUSDT'],
    timeframe: '1h',
    started_at: null,
  },
];

function selectByLabel(container: HTMLElement, label: string): HTMLSelectElement {
  const el = Array.from(container.querySelectorAll('label')).find((l) => l.textContent === label);
  const select = el ? document.getElementById(el.htmlFor) : null;
  if (!(select instanceof HTMLSelectElement)) throw new Error(`no select ${label}`);
  return select;
}

async function choose(select: HTMLSelectElement, value: string): Promise<void> {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
  await act(async () => {
    setter?.call(select, value);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

function posts(path: string): unknown[] {
  return calls
    .filter((c) => c.path === path && c.init?.method && c.init.method !== 'GET')
    .map((c) => (c.init?.body ? JSON.parse(String(c.init.body)) : null));
}

describe('TradingPage without the platform config', () => {
  it('shows UNAVAILABLE and requests no source-specific data instead of guessing paper', async () => {
    const routes = baseRoutes();
    routes['/api/system/config'] = unavailable('system_config has no row for: trading_mode');
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TradingPage />);

    expect(calls.some((c) => c.path.includes('source='))).toBe(false);
    const positions = sectionText(page.container, 'Open Positions');
    expect(positions).toContain('UNAVAILABLE');
    expect(positions).toContain('system_config has no row for: trading_mode');
    expect(sectionText(page.container, 'Daily PnL vs Limit')).toContain('UNAVAILABLE');
  });
});

describe('TradingPage risk status before the first mark', () => {
  it('shows not measured instead of a $0 loss when daily_loss is null', async () => {
    const routes = baseRoutes();
    routes['/api/risk/status?source=paper'] = {
      current_drawdown: 0,
      max_drawdown_limit: 15,
      daily_loss: null,
      daily_loss_limit: null,
      circuit_breakers: null,
    };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TradingPage />);

    const row = sectionText(page.container, 'Daily PnL vs Limit');
    expect(row).toContain('Not measured yet');
    expect(row).not.toContain('$0.00');
  });

  it('shows a tripped breaker as Tripped', async () => {
    const routes = baseRoutes();
    routes['/api/risk/circuit-breakers'] = [
      { tier: 3, label: 'Flatten', threshold: '10.00% drawdown', current_value: 11, status: 'Tripped' },
    ];
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TradingPage />);

    expect(page.container.textContent).toContain('Tier 3: Flatten');
    expect(page.container.textContent).toContain('Tripped');
  });
});

describe('TradingPage quick trade', () => {
  it('offers running paper sessions only and blocks until one is selected', async () => {
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = SESSIONS;
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TradingPage />);

    const session = selectByLabel(page.container, 'Session');
    expect(Array.from(session.options).map((o) => o.value)).toEqual(['', 'sess-paper']);
    const review = Array.from(page.container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Review Buy'),
    );
    expect(review?.disabled).toBe(true);
  });

  it('confirms PAPER, strategy and notional, then posts session_id', async () => {
    const ask = vi.fn(() => true);
    vi.stubGlobal('confirm', ask);
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = SESSIONS;
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TradingPage />);

    await choose(selectByLabel(page.container, 'Session'), 'sess-paper');
    await typeInto(inputByLabel(page.container, 'Quantity'), '0.01');
    await typeInto(inputByLabel(page.container, 'Stop Loss'), '60000');
    const review = Array.from(page.container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Review Buy'),
    );
    await click(review as HTMLButtonElement);

    const message = String((ask.mock.calls[0] as unknown[])[0]);
    expect(message).toContain('PAPER ORDER');
    expect(message).toContain('Momentum BTC');
    expect(message).toContain('BUY 0.01 BTCUSDT MARKET');
    expect(message).toContain('Estimated notional: $650.01');
    expect(posts('/api/trading/quick-trade')).toEqual([
      {
        session_id: 'sess-paper',
        symbol: 'BTCUSDT',
        side: 'buy',
        quantity: 0.01,
        order_type: 'market',
        stop_loss: 60000,
      },
    ]);
  });

  it.each([
    ['sess-paper', 'success', 'PAPER BUY order placed for 0.01 BTCUSDT'],
    ['sess-other', 'error', 'placed on session sess-other, not sess-paper'],
  ])('checks the session the server used (%s)', async (used, level, text) => {
    vi.stubGlobal('confirm', vi.fn(() => true));
    toast.mockReset();
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = SESSIONS;
    const base = routeFetch(routes, calls);
    fetchApi.mockImplementation(async (path: string, init?: RequestInit) =>
      path === '/api/trading/quick-trade'
        ? { order_id: 'o1', session_id: used, status: 'filled' }
        : base(path, init),
    );
    page = await mount(<TradingPage />);

    await choose(selectByLabel(page.container, 'Session'), 'sess-paper');
    await typeInto(inputByLabel(page.container, 'Quantity'), '0.01');
    const review = Array.from(page.container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Review Buy'),
    );
    await click(review as HTMLButtonElement);

    const [lastLevel, lastText] = toast.mock.calls.at(-1) as [string, string];
    expect(lastLevel).toBe(level);
    expect(lastText).toContain(text);
  });

  it('sends nothing when the confirmation is declined', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false));
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = SESSIONS;
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TradingPage />);

    await choose(selectByLabel(page.container, 'Session'), 'sess-paper');
    await typeInto(inputByLabel(page.container, 'Quantity'), '0.01');
    const review = Array.from(page.container.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Review Buy'),
    );
    await click(review as HTMLButtonElement);

    expect(posts('/api/trading/quick-trade')).toEqual([]);
  });
});

describe('TradingPage kill switch', () => {
  it('asks before activating', async () => {
    const ask = vi.fn(() => false);
    vi.stubGlobal('confirm', ask);
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<TradingPage />);

    await click(buttonByText(page.container, 'Activate'));

    expect(ask).toHaveBeenCalled();
    expect(posts('/api/trading/kill-switch')).toEqual([]);
  });

  it('releases only after RELEASE is typed, with the reason', async () => {
    const routes = baseRoutes();
    routes['/api/risk/config'] = { kill_switch_active: true };
    fetchApi.mockImplementation(routeFetch(routes, calls));

    vi.stubGlobal('prompt', vi.fn(() => 'release'));
    page = await mount(<TradingPage />);
    await click(buttonByText(page.container, 'Deactivate'));
    expect(posts('/api/trading/kill-switch')).toEqual([]);

    const answers = ['RELEASE', 'breaker reviewed'];
    vi.stubGlobal('prompt', vi.fn(() => answers.shift()));
    await click(buttonByText(page.container, 'Deactivate'));
    expect(posts('/api/trading/kill-switch')).toEqual([
      { confirm: 'RELEASE', reason: 'breaker reviewed' },
    ]);
  });
});

describe('TradingPage chart', () => {
  it('embeds TradingView as a cross-origin iframe, never as a script in this origin', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<TradingPage />);

    const frame = page.container.querySelector('iframe');
    expect(frame).not.toBeNull();
    expect(new URL(frame?.getAttribute('src') ?? '').origin).toBe('https://s.tradingview.com');
    expect(frame?.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin allow-popups');
    expect(document.querySelectorAll('script[src*="tradingview"]').length).toBe(0);
  });
});
