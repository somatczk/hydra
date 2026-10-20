import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';

const fetchApi = vi.fn();
const toast = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast }) }));
const searchParams = new URLSearchParams('strategy=strat-1');
const router = { push: vi.fn() };
vi.mock('next/navigation', () => ({
  useRouter: () => router,
  useSearchParams: () => searchParams,
}));

const { default: BuilderPage } = await import('./page');

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

function routes(): Routes {
  return {
    '/api/strategies/indicators': [],
    '/api/strategies/comparators': [],
    '/api/models': [],
    '/api/strategies/strat-1': new ApiError(
      404,
      JSON.stringify({
        detail: { code: 'strategy_not_found', message: 'Strategy strat-1 not found', state: 'not_found' },
      }),
    ),
  };
}

beforeEach(() => {
  calls = [];
  fetchApi.mockReset();
  toast.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('BuilderPage error paths', () => {
  it('shows the 404 for an unknown strategy instead of the raw response body', async () => {
    fetchApi.mockImplementation(routeFetch(routes(), calls));
    page = await mount(<BuilderPage />);

    const card = page.container.querySelector('[data-testid="load-error"]');
    expect(card?.textContent).toContain('Strategy strat-1 not found');
    expect(card?.querySelector('[data-state="not_found"]')).not.toBeNull();
    expect(toast).toHaveBeenCalledWith('error', 'Failed to load strategy: Strategy strat-1 not found');
  });

  it('says when the indicator catalogue falls back to the built-in list', async () => {
    const r = routes();
    r['/api/strategies/indicators'] = unavailable();
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mount(<BuilderPage />);

    expect(toast).toHaveBeenCalledWith(
      'warning',
      'Using built-in indicator list; server catalogue unavailable (indicators: Database is not available)',
    );
  });
});
