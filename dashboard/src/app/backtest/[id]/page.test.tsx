import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useParams: () => ({ id: 'bt-task-1' }),
}));

const { default: BacktestDetailPage } = await import('./page');

const DETAIL = {
  id: 'bt-task-1',
  strategy: 'Saw',
  period: '2026-01-01T00:00:00+00:00 - 2026-01-13T11:00:00+00:00',
  status: 'completed',
  name: 'real',
  stopped_reason: null,
  provenance: {
    exchange: 'binance',
    symbol: 'BTCUSDT',
    timeframe: '1h',
    bar_count: 300,
    first_ts: '2026-01-01T00:00:00+00:00',
    last_ts: '2026-01-13T11:00:00+00:00',
    gap_count: 3,
    requested_start: '2026-01-01T00:00:00+00:00',
    requested_end: '2026-01-13T23:59:59.999999+00:00',
    data_source: 'db',
  },
  metrics: { total_trades: 0, win_rate: 0, total_pnl: 0, max_drawdown: 0, sharpe_ratio: 0 },
  equity_curve: [],
  benchmark_equity: [],
  trades: [],
  transactions: [],
};

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

function routes(detail: unknown): Routes {
  return {
    '/api/backtest/results/bt-task-1': detail,
    '/api/backtest/results/bt-task-1/verify': new ApiError(404, '{}'),
  };
}

beforeEach(() => {
  calls = [];
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('BacktestDetailPage', () => {
  it('shows the bars the result ran on, with coverage warnings', async () => {
    fetchApi.mockImplementation(routeFetch(routes(DETAIL), calls));
    page = await mount(<BacktestDetailPage />);

    const panel = page.container.querySelector('[data-testid="provenance-panel"]');
    expect(panel?.textContent).toContain('binance BTCUSDT 1h');
    expect(panel?.textContent).toContain('3 gaps in the stored bars');
    expect(panel?.textContent).toContain('before the requested end');
  });

  it('warns when a result has no provenance', async () => {
    fetchApi.mockImplementation(routeFetch(routes({ ...DETAIL, provenance: null }), calls));
    page = await mount(<BacktestDetailPage />);

    expect(page.container.textContent).toContain('No data provenance recorded');
    expect(page.container.querySelector('[data-state="unknown"]')).not.toBeNull();
  });

  it('shows a persisted margin call and its extended metrics', async () => {
    const stopped = {
      ...DETAIL,
      stopped_reason: 'margin_call',
      metrics: {
        ...DETAIL.metrics,
        alpha: -0.31,
        beta: 1.7,
        max_consecutive_wins: 1,
        max_consecutive_losses: 2,
        expectancy: -316.67,
      },
    };
    fetchApi.mockImplementation(routeFetch(routes(stopped), calls));
    page = await mount(<BacktestDetailPage />);

    const text = page.container.textContent ?? '';
    expect(text).toMatch(/margin call/i);
    expect(text).toContain('-0.31');
    expect(text).toContain('1.70');
    expect(text).toContain('-316.67');
  });

  it('hides extended metrics that were not recorded instead of showing zeros', async () => {
    const unrecorded = {
      ...DETAIL,
      metrics: {
        ...DETAIL.metrics,
        alpha: null,
        beta: null,
        max_consecutive_wins: null,
        max_consecutive_losses: null,
        expectancy: null,
      },
    };
    fetchApi.mockImplementation(routeFetch(routes(unrecorded), calls));
    page = await mount(<BacktestDetailPage />);

    const text = page.container.textContent ?? '';
    expect(text).not.toContain('Alpha');
    expect(text).not.toContain('Expectancy');
    expect(text).not.toContain('Max Consec. Losses');
  });

  it('distinguishes a missing result from an outage', async () => {
    const notFound = new ApiError(
      404,
      JSON.stringify({ detail: { code: 'result_not_found', message: 'Result bt-task-1 not found', state: 'not_found' } }),
    );
    fetchApi.mockImplementation(routeFetch(routes(notFound), calls));
    page = await mount(<BacktestDetailPage />);
    expect(page.container.textContent).toContain('Backtest not found');
    page.unmount();

    fetchApi.mockImplementation(routeFetch(routes(unavailable()), calls));
    page = await mount(<BacktestDetailPage />);
    expect(page.container.textContent).toContain('Database is not available');
    expect(page.container.textContent).not.toContain('Backtest not found');
  });
});
