import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buttonByText, click, mount, routeFetch, unavailable, type Mounted } from '@/test/pageHarness';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));

const { default: PortfolioPage } = await import('./page');

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

beforeEach(() => {
  calls = [];
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('PortfolioPage without the platform config', () => {
  it('requests no ?source= data and shows the reason', async () => {
    fetchApi.mockImplementation(
      routeFetch(
        {
          '/api/system/config': unavailable('system_config has no row for: trading_mode'),
          '/api/market/sentiment': [],
          '/api/strategies': [],
        },
        calls,
      ),
    );
    page = await mount(<PortfolioPage />);

    expect(calls.some((c) => c.path.includes('source='))).toBe(false);
    expect(page.container.textContent).toContain('UNAVAILABLE');
    expect(page.container.textContent).toContain('system_config has no row for: trading_mode');
  });
});

const SUMMARY = {
  total_value: 10012.5,
  unrealized_pnl: 1.5,
  realized_pnl: -12.5,
  daily_pnl: 0,
  max_drawdown_pct: 0.2,
  total_fees: 3.25,
  change_pct: 0.1,
  paper_capital_pending: null,
};

function paperRoutes() {
  return {
    '/api/system/config': { trading_mode: 'paper' },
    '/api/portfolio/summary?source=paper': SUMMARY,
    '/api/portfolio/positions?source=paper': [
      {
        id: 'p1',
        pair: 'BTC/USDT',
        exchange: 'binance',
        side: 'Long',
        size: 0.0015,
        entry_price: 64000,
        current_price: 65000,
        unrealized_pnl: 1.5,
        pnl_pct: 1.5,
      },
    ],
    '/api/portfolio/equity-curve?source=paper': [],
    '/api/portfolio/daily-pnl?source=paper': [],
    '/api/portfolio/monthly-returns?source=paper': [],
    '/api/portfolio/attribution?source=paper': [],
    '/api/strategies': [{ id: 'saw', name: 'Saw' }],
    '/api/portfolio/trades?page=1&source=paper': {
      trades: [
        {
          id: 't1',
          strategy_id: 'saw',
          symbol: 'BTCUSDT',
          side: 'sell',
          timestamp: '2026-10-20T12:00:00+00:00',
          price: 65000,
          quantity: 0.0015,
          pnl: -12.5,
          notes: '',
          tags: [],
        },
      ],
      total: 1,
      page: 1,
      limit: 20,
      pages: 1,
    },
  };
}

describe('PortfolioPage as the Performance screen', () => {
  it('has one h1, carries source=paper on every portfolio call and never asks for sentiment', async () => {
    fetchApi.mockImplementation(routeFetch(paperRoutes(), calls));
    page = await mount(<PortfolioPage />);
    await click(buttonByText(page.container, 'Apply filters'));

    expect(page.container.querySelectorAll('h1')).toHaveLength(1);
    expect(page.container.querySelector('h1')?.textContent).toBe('Performance');
    const portfolioCalls = calls.filter((c) => c.path.startsWith('/api/portfolio/'));
    expect(portfolioCalls.length).toBeGreaterThanOrEqual(7);
    expect(portfolioCalls.every((c) => c.path.includes('source=paper'))).toBe(true);
    expect(calls.some((c) => c.path.includes('sentiment'))).toBe(false);
    expect(page.container.textContent).not.toContain('Sentiment');
  });

  it('puts the export below the summary readouts', async () => {
    fetchApi.mockImplementation(routeFetch(paperRoutes(), calls));
    page = await mount(<PortfolioPage />);

    const text = page.container.textContent ?? '';
    expect(text.indexOf('Total value')).toBeGreaterThan(-1);
    expect(text.indexOf('Total value')).toBeLessThan(text.indexOf('Export trades'));
  });

  it('labels the position mark honestly and shows a long as Held', async () => {
    fetchApi.mockImplementation(routeFetch(paperRoutes(), calls));
    page = await mount(<PortfolioPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Mark time not reported');
    const headers = Array.from(page.container.querySelectorAll('th')).map((th) => th.textContent);
    expect(headers).toContain('Last stored close');
    const row = page.container.querySelector('tbody tr')?.textContent ?? '';
    expect(row).toContain('Held');
    expect(row).not.toContain('Long');
    expect(row).toContain('65,000.00');
    expect(row).toContain('0.00150');
  });

  it('reads timestamp and strategy_id in the journal and signs a loss', async () => {
    fetchApi.mockImplementation(routeFetch(paperRoutes(), calls));
    page = await mount(<PortfolioPage />);
    await click(buttonByText(page.container, 'Apply filters'));

    const journal = Array.from(page.container.querySelectorAll('section')).find((sec) =>
      sec.textContent?.startsWith('Trade journal'),
    );
    const row = journal?.querySelector('tbody tr')?.textContent ?? '';
    expect(row).toContain('12:00 UTC');
    expect(row).toContain('Saw');
    expect(row).toContain('Sell');
    expect(row).toContain('−12.50');
    expect(row).toContain('loss 12.50 USDT');
    expect(row).not.toContain('Invalid');
  });
});

describe('PortfolioPage source pinning, export scope and chart data', () => {
  it('pins source=paper even when a stored mode says live (the mode no longer derives from config)', async () => {
    const routes = { ...paperRoutes(), '/api/system/config': { trading_mode: 'live' } };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<PortfolioPage />);

    const portfolioCalls = calls.filter((c) => c.path.startsWith('/api/portfolio/'));
    expect(portfolioCalls.length).toBeGreaterThanOrEqual(6);
    expect(portfolioCalls.every((c) => c.path.includes('source=paper'))).toBe(true);
    expect(calls.some((c) => c.path.includes('source=live'))).toBe(false);
    expect(page.container.querySelector('[data-testid="stored-mode-note"]')).toBeNull();
  });

  it('scopes the export to the paper source', async () => {
    fetchApi.mockImplementation(routeFetch(paperRoutes(), calls));
    const fetchMock = vi.fn().mockResolvedValue(new Response('x', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    URL.createObjectURL = vi.fn(() => 'blob:x');
    URL.revokeObjectURL = vi.fn();
    page = await mount(<PortfolioPage />);
    await click(buttonByText(page.container, 'Export'));

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/portfolio/export/csv?format=generic&source=paper');
    vi.unstubAllGlobals();
  });

  it('offers the equity and daily P&L points as tables', async () => {
    const routes = {
      ...paperRoutes(),
      '/api/portfolio/equity-curve?source=paper': [
        { timestamp: '2026-10-19T00:00:00+00:00', value: 10000 },
        { timestamp: '2026-10-20T00:00:00+00:00', value: 10012.5 },
      ],
      '/api/portfolio/daily-pnl?source=paper': [{ date: '2026-10-20', pnl: 12.5 }],
    };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<PortfolioPage />);

    const equity = page.container.querySelector('[data-testid="equity-table"]');
    expect(equity?.querySelectorAll('tbody tr')).toHaveLength(2);
    expect(equity?.textContent).toContain('10,012.50 USDT');
    const daily = page.container.querySelector('[data-testid="daily-pnl-table"]');
    expect(daily?.textContent).toContain('2026-10-20');
    expect(daily?.textContent).toContain('12.50');
  });
});
