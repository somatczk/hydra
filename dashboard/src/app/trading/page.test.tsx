import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, routeFetch, sectionText, unavailable, type Mounted, type Routes } from '@/test/pageHarness';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));
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
