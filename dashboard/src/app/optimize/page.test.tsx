import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, mount, type Mounted } from '@/test/pageHarness';

const fetchApi = vi.fn();
const toast = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast }) }));
const router = { push: vi.fn() };
vi.mock('next/navigation', () => ({ useRouter: () => router }));

const { default: OptimizePage } = await import('./page');

const NO_DATA = new ApiError(
  422,
  JSON.stringify({
    detail: {
      code: 'no_market_data',
      message: 'No market data for binance BTCUSDT 1h between X and Y; available: none stored',
      state: 'no_data',
    },
  }),
);

function serve(overrides: Record<string, () => unknown> = {}) {
  const defaults: Record<string, () => unknown> = {
    'GET /api/backtest/hyperopt/history': () => [],
    'GET /api/backtest/strategies': () => [{ id: 'saw', name: 'Saw', primary_timeframe: '1h' }],
    'GET /api/backtest/strategies/saw/params': () => [
      { name: 'sma_period', label: 'SMA Period', type: 'int', default: 5, min: 2, max: 20, step: 1 },
    ],
    'POST /api/backtest/hyperopt': () => NO_DATA,
  };
  fetchApi.mockImplementation(async (path: string, init?: RequestInit) => {
    const key = `${init?.method ?? 'GET'} ${path}`;
    const value = (overrides[key] ?? defaults[key])?.();
    if (value instanceof Error) throw value;
    if (value === undefined) throw new ApiError(404, JSON.stringify({ detail: 'no route' }));
    return value;
  });
}

let page: Mounted | null = null;

beforeEach(() => {
  fetchApi.mockReset();
  toast.mockReset();
  localStorage.clear();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('OptimizePage', () => {
  it('shows the no-data reason when an optimization cannot start', async () => {
    serve();
    page = await mount(<OptimizePage />);

    await click(buttonByText(page.container, 'Run Optimization'));

    const card = page.container.querySelector('[data-testid="run-error"]');
    expect(card?.textContent).toContain('No market data for binance BTCUSDT 1h');
    expect(card?.querySelector('[data-state="no_data"]')).not.toBeNull();
  });

  it('says the strategy is unknown when its params 404', async () => {
    serve({
      'GET /api/backtest/strategies/saw/params': () =>
        new ApiError(
          404,
          JSON.stringify({ detail: { code: 'strategy_not_found', message: 'Strategy saw not found', state: 'not_found' } }),
        ),
    });
    page = await mount(<OptimizePage />);

    expect(page.container.textContent).toContain('Strategy not found');
    expect(page.container.textContent).not.toContain('No optimizable parameters found');
  });
});
