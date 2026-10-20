import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, routeFetch, unavailable, type Mounted } from '@/test/pageHarness';

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
