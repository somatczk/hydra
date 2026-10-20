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
});
