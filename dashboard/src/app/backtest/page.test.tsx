import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, flush, mount, unavailable, type Mounted } from '@/test/pageHarness';

const fetchApi = vi.fn();
const toast = vi.fn();
const nav = vi.hoisted(() => ({ search: new URLSearchParams() }));

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast }) }));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useSearchParams: () => nav.search,
}));

const { default: BacktestPage } = await import('./page');

const NO_DATA = new ApiError(
  422,
  JSON.stringify({
    detail: {
      code: 'no_market_data',
      message:
        'No market data for binance BTCUSDT 4h between 2026-01-01T00:00:00+00:00 and 2026-03-01T23:59:59.999999+00:00; available: none stored',
      state: 'no_data',
      available_first_ts: null,
      available_last_ts: null,
    },
  }),
);

const RESULT = {
  id: 'bt-task-1',
  strategy: 'Saw',
  period: '2026-01-01T00:00:00+00:00 - 2026-01-13T11:00:00+00:00',
  status: 'completed',
  name: 'real',
  strategy_id: 'saw',
  provenance: {
    exchange: 'binance',
    symbol: 'BTCUSDT',
    timeframe: '1h',
    bar_count: 300,
    first_ts: '2026-01-01T00:00:00+00:00',
    last_ts: '2026-01-13T11:00:00+00:00',
    gap_count: 0,
    data_source: 'db',
  },
  metrics: { total_trades: 14, win_rate: 0, total_pnl: -50, max_drawdown: 1, sharpe_ratio: 0 },
};

type Handler = (path: string, init?: RequestInit) => unknown;

let page: Mounted | null = null;
let posts: Array<Record<string, unknown>>;

function serve(overrides: Record<string, Handler> = {}) {
  fetchApi.mockImplementation(async (path: string, init?: RequestInit) => {
    const key = `${init?.method ?? 'GET'} ${path.split('?')[0]}`;
    if (init?.method === 'POST') posts.push(JSON.parse(String(init.body)));
    const handler = overrides[key];
    const value = handler
      ? handler(path, init)
      : {
          'GET /api/backtest/results': [RESULT],
          'GET /api/backtest/strategies': [{ id: 'saw', name: 'Saw', primary_timeframe: '4h' }],
        }[key];
    if (value instanceof Error) throw value;
    if (value === undefined) throw new ApiError(404, JSON.stringify({ detail: 'no route' }));
    return value;
  });
}

beforeEach(() => {
  nav.search = new URLSearchParams();
  posts = [];
  fetchApi.mockReset();
  toast.mockReset();
  localStorage.clear();
});

afterEach(() => {
  page?.unmount();
  page = null;
  vi.useRealTimers();
});

describe('BacktestPage', () => {
  it('pre-selects the strategy primary timeframe and has no 30m option', async () => {
    serve({ 'POST /api/backtest/run': () => NO_DATA });
    page = await mount(<BacktestPage />);

    const options = Array.from(page.container.querySelectorAll('option')).map((o) => o.value);
    expect(options).not.toContain('30m');
    expect(options).toContain('1w');

    await click(buttonByText(page.container, 'Run Backtest'));
    expect(posts[0]).toMatchObject({ strategy_id: 'saw', timeframe: '4h', start_date: '2026-01-01' });
  });

  it('Save & Backtest autorun waits for the strategy primary timeframe', async () => {
    nav.search = new URLSearchParams('strategy=saw&autorun=true');
    serve({ 'POST /api/backtest/run': () => NO_DATA });
    page = await mount(<BacktestPage />);
    await flush();

    expect(posts).toHaveLength(1);
    expect(posts[0]).toMatchObject({ strategy_id: 'saw', timeframe: '4h' });
  });

  it('autorun does not run a different strategy when the requested one is unknown', async () => {
    nav.search = new URLSearchParams('strategy=gone&autorun=true');
    serve({ 'POST /api/backtest/run': () => NO_DATA });
    page = await mount(<BacktestPage />);
    await flush();

    expect(posts).toHaveLength(0);
  });

  it('shows the 422 no-data message with the stored range, persistently', async () => {
    serve({ 'POST /api/backtest/run': () => NO_DATA });
    page = await mount(<BacktestPage />);

    await click(buttonByText(page.container, 'Run Backtest'));

    const card = page.container.querySelector('[data-testid="run-error"]');
    expect(card?.textContent).toContain('No market data for binance BTCUSDT 4h');
    expect(card?.textContent).toContain('available: none stored');
    expect(card?.querySelector('[data-state="no_data"]')?.textContent).toBe('NO DATA');
    expect(card?.textContent).not.toContain('{');
  });

  it('shows the task error when the run fails in the background', async () => {
    vi.useFakeTimers();
    serve({
      'POST /api/backtest/run': () => ({ task_id: 'task-1', status: 'queued' }),
      'GET /api/backtest/status/task-1': () => ({
        task_id: 'task-1',
        status: 'failed',
        progress: 0,
        error: 'result not persisted: connection was closed',
      }),
    });
    page = await mount(<BacktestPage />);

    await click(buttonByText(page.container, 'Run Backtest'));
    await act(async () => {
      vi.advanceTimersByTime(1600);
    });
    await flush();

    expect(page.container.querySelector('[data-testid="run-failure"]')?.textContent).toContain(
      'Backtest failed: result not persisted: connection was closed',
    );
    expect(toast).toHaveBeenCalledWith('error', 'Backtest failed: result not persisted: connection was closed');
  });

  it('marks each result with its data source', async () => {
    serve();
    page = await mount(<BacktestPage />);
    expect(page.container.querySelector('[data-state="db"]')?.textContent).toBe('DB');
  });

  it('reports an unavailable results list instead of an empty table', async () => {
    serve({ 'GET /api/backtest/results': () => unavailable() });
    page = await mount(<BacktestPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Database is not available');
    expect(page.container.querySelector('[data-state="unavailable"]')).not.toBeNull();
  });
});

describe('BacktestPage history values', () => {
  it('shows a losing result with its real numbers, never N/A', async () => {
    serve();
    page = await mount(<BacktestPage />);

    const row = page.container.querySelector('tbody tr')?.textContent ?? '';
    expect(row).toContain('−50.00');
    expect(row).toContain('0%');
    expect(row).toContain('0.00');
    expect(row).not.toContain('N/A');
  });

  it('shows a missing metric as a dash with NO DATA', async () => {
    serve({
      'GET /api/backtest/results': () => [
        { ...RESULT, metrics: { ...RESULT.metrics, win_rate: null, sharpe_ratio: null } },
      ],
    });
    page = await mount(<BacktestPage />);

    const cells = Array.from(page.container.querySelectorAll('tbody tr td')).map((td) => td.textContent);
    expect(cells.filter((c) => c === '— NO DATA')).toHaveLength(2);
  });

  it('has one h1, the Research nav and a real link to the result', async () => {
    serve();
    page = await mount(<BacktestPage />);

    expect(page.container.querySelectorAll('h1')).toHaveLength(1);
    expect(page.container.querySelector('nav[aria-label="Research"] [aria-current="page"]')?.textContent).toBe('Backtests');
    expect(page.container.querySelector('tbody a[href="/backtest/bt-task-1"]')?.textContent).toBe('View');
  });
});
