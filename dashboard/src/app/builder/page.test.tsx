import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { click, mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';

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

describe('BuilderPage scope', () => {
  it('has one h1 and no DCA, Grid or ML overlay controls', async () => {
    fetchApi.mockImplementation(routeFetch(routes(), calls));
    page = await mount(<BuilderPage />);

    expect(page.container.querySelectorAll('h1')).toHaveLength(1);
    const text = page.container.textContent ?? '';
    expect(text).not.toContain('DCA');
    expect(text).not.toContain('Grid Bot');
    expect(text).not.toContain('ML Model Overlay');
    expect(calls.some((c) => c.path === '/api/models')).toBe(false);
  });

  it('keeps ml_overlay when a strategy that has one is saved', async () => {
    const r = routes();
    r['/api/strategies/strat-1'] = {
      id: 'strat-1',
      name: 'Saw',
      description: 'd',
      enabled: false,
      exchange_id: 'binance',
      symbol: 'BTCUSDT',
      rules: { entry_long: null, exit_long: null, entry_short: null, exit_short: null },
      timeframes: { primary: '1h' },
      risk: {
        stop_loss_method: 'atr',
        stop_loss_value: 2,
        take_profit_method: 'atr',
        take_profit_value: 3,
        sizing_method: 'fixed_fractional',
        sizing_params: {},
      },
      ml_overlay: { model_name: 'm1', confidence_threshold: 0.6 },
    };
    fetchApi.mockImplementation(routeFetch(r, calls, { 'PUT /api/strategies/strat-1': { id: 'strat-1' } }));
    page = await mount(<BuilderPage />);

    const update = () => Array.from(page!.container.querySelectorAll('button')).filter((b) => b.textContent?.trim() === 'Update Strategy');
    await click(update()[0]);
    await click(update()[update().length - 1]);

    const put = calls.find((c) => c.init?.method === 'PUT');
    expect(JSON.parse(String(put?.init?.body)).ml_overlay).toEqual({ model_name: 'm1', confidence_threshold: 0.6 });
  });
});
