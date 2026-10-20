import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, routeFetch, unavailable, type Mounted } from '@/test/pageHarness';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { default: DashboardPage } = await import('./page');

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

describe('DashboardPage without the platform config', () => {
  it('shows why instead of loading paper data under a guessed source', async () => {
    fetchApi.mockImplementation(
      routeFetch(
        {
          '/api/system/config': unavailable('system_config has no row for: trading_mode'),
          '/api/market/news': [],
        },
        calls,
      ),
    );
    page = await mount(<DashboardPage />);

    expect(calls.map((c) => c.path)).toEqual(['/api/system/config', '/api/market/news']);
    expect(page.container.textContent).toContain('UNAVAILABLE');
    expect(page.container.textContent).toContain('system_config has no row for: trading_mode');
  });

  it('loads the configured source when the config is readable', async () => {
    fetchApi.mockImplementation(routeFetch({ '/api/system/config': { trading_mode: 'paper' } }, calls));
    page = await mount(<DashboardPage />);

    expect(calls.some((c) => c.path === '/api/portfolio/summary?source=paper')).toBe(true);
    expect(calls.some((c) => c.path === '/api/portfolio/live-metrics?source=paper')).toBe(true);
  });

  it('says when a paper capital change is still pending on the ledger', async () => {
    const summary = {
      total_value: 10100,
      unrealized_pnl: 0,
      realized_pnl: 0,
      daily_pnl: 0,
      max_drawdown_pct: 0,
      total_fees: 0,
      change_pct: 0,
      paper_capital_pending: 25000,
    };
    fetchApi.mockImplementation(
      routeFetch(
        {
          '/api/system/config': { trading_mode: 'paper' },
          '/api/portfolio/summary?source=paper': summary,
        },
        calls,
      ),
    );
    page = await mount(<DashboardPage />);

    const note = page.container.querySelector('[data-testid="capital-pending"]')?.textContent ?? '';
    expect(note).toContain('Paper capital change to $25,000 is pending');
    expect(page.container.textContent).toContain('$10,100.00');
  });
});
