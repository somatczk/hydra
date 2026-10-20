import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { mount, routeFetch, unavailable, type Mounted } from '@/test/pageHarness';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
const router = { push: vi.fn() };
const params = { taskId: 'ho-1' };
vi.mock('next/navigation', () => ({ useRouter: () => router, useParams: () => params }));

const { default: HyperoptDetailPage } = await import('./page');

const FAILED = {
  task_id: 'ho-1',
  status: 'failed',
  strategy_id: 'saw',
  created_at: '2026-10-20T10:00:00+00:00',
  completed_trials: 0,
  total_trials: 2,
  best_so_far: null,
  error: 'No market data for binance BTCUSDT 1h; available: none stored',
  provenance: null,
};

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

describe('HyperoptDetailPage', () => {
  it('shows why a run failed', async () => {
    fetchApi.mockImplementation(routeFetch({ '/api/backtest/hyperopt/ho-1': FAILED }, calls));
    page = await mount(<HyperoptDetailPage />);

    expect(page.container.textContent).toContain(
      'This hyperopt run failed: No market data for binance BTCUSDT 1h; available: none stored',
    );
    expect(page.container.textContent).toContain('Strategy: saw');
  });

  it('shows the provenance of a completed run', async () => {
    const provenance = {
      exchange: 'binance',
      symbol: 'BTCUSDT',
      timeframe: '1h',
      bar_count: 300,
      first_ts: '2026-01-01T00:00:00+00:00',
      last_ts: '2026-01-13T11:00:00+00:00',
      gap_count: 0,
      data_source: 'db',
    };
    fetchApi.mockImplementation(
      routeFetch(
        {
          '/api/backtest/hyperopt/ho-1': { ...FAILED, status: 'completed', error: null, provenance },
          '/api/backtest/hyperopt/ho-1/results': {
            task_id: 'ho-1',
            status: 'completed',
            best_params: { sma_period: 5 },
            best_metric: 0.5,
            trials: [],
            total_trials: 2,
            completed_trials: 2,
            provenance,
          },
        },
        calls,
      ),
    );
    page = await mount(<HyperoptDetailPage />);

    expect(page.container.querySelector('[data-testid="provenance-panel"]')?.textContent).toContain('300');
  });

  it('separates an unknown run from an outage', async () => {
    const notFound = new ApiError(
      404,
      JSON.stringify({ detail: { code: 'task_not_found', message: 'Hyperopt task ho-1 not found', state: 'not_found' } }),
    );
    fetchApi.mockImplementation(routeFetch({ '/api/backtest/hyperopt/ho-1': notFound }, calls));
    page = await mount(<HyperoptDetailPage />);
    expect(page.container.textContent).toContain('Hyperopt run not found');
    page.unmount();

    fetchApi.mockImplementation(routeFetch({ '/api/backtest/hyperopt/ho-1': unavailable() }, calls));
    page = await mount(<HyperoptDetailPage />);
    expect(page.container.textContent).toContain('Database is not available');
  });
});
