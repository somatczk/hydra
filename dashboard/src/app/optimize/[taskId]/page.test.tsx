import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import type { ResearchJob } from '@/lib/research';
import { advance, buttonByText, click, mount, unavailable, type Mounted } from '@/test/pageHarness';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
const router = vi.hoisted(() => ({ push: vi.fn() }));
const params = { taskId: 'ho-1' };
vi.mock('next/navigation', () => ({ useRouter: () => router, useParams: () => params }));

const { default: OptimizeTaskPage } = await import('./page');

const HYPEROPT = {
  task_id: 'ho-1',
  status: 'completed',
  strategy_id: 'saw',
  created_at: '2026-10-20T10:00:00+00:00',
  completed_trials: 12,
  total_trials: 14,
  error: null,
};

const TRIALS = [
  {
    trial_number: 1,
    params: { sma_period: 42 },
    sharpe: 1.234,
    total_return: 0.12,
    max_drawdown: 0.05,
    total_trades: 18,
    status: 'ok',
    error: null,
  },
  {
    trial_number: 2,
    params: { sma_period: 7 },
    sharpe: 0,
    total_return: 0,
    max_drawdown: 0,
    total_trades: 0,
    status: 'failed',
    error: 'non-finite Sharpe ratio: nan',
  },
  {
    trial_number: 3,
    params: { sma_period: 9 },
    sharpe: 0,
    total_return: 0,
    max_drawdown: 0,
    total_trades: 0,
    status: 'no_trades',
    error: 'strategy made no trades',
  },
];

function hyperoptResult(overrides: Record<string, unknown> = {}) {
  return {
    task_id: 'ho-1',
    status: 'completed',
    best_params: { sma_period: 42 },
    best_metric: 1.253,
    trials: TRIALS,
    total_trials: 14,
    completed_trials: 12,
    n_trials: 10,
    failed_trials: 2,
    seed: 42,
    train_start: '2025-01-01T00:00:00+00:00',
    train_end: '2026-03-01T00:00:00+00:00',
    holdout_start: '2026-03-01T00:00:00+00:00',
    holdout_end: '2026-04-01T00:00:00+00:00',
    holdout: { sharpe: 0.9, total_return: 0.04, max_drawdown: 0.06, total_trades: 4 },
    holdout_psr: 0.62,
    dsr: 0.41,
    pbo: 0.3,
    provenance: null,
    ...overrides,
  };
}

const WF_RESULT = {
  task_id: 'wf-1',
  status: 'completed',
  folds: [
    {
      fold_index: 0,
      in_sample_sharpe: 1.2,
      in_sample_return: 0.08,
      in_sample_drawdown: 0.03,
      in_sample_trades: 12,
      out_of_sample_sharpe: 0.9,
      out_of_sample_return: 0.04,
      out_of_sample_drawdown: 0.05,
      out_of_sample_trades: 9,
      selected_params: { period: 42 },
      n_trials: 7,
      is_start: '2026-01-01T00:00:00+00:00',
      is_end: '2026-03-01T00:00:00+00:00',
      oos_start: '2026-03-01T00:00:00+00:00',
      oos_end: '2026-03-31T00:00:00+00:00',
    },
  ],
  aggregated_sharpe: 0.9,
  aggregated_return: 0.04,
  aggregated_drawdown: 0.05,
  out_of_sample_equity: [10000, 10100, 10200],
  oos_psr: 0.55,
  efficiency: 0.75,
  anchored: true,
  seed: 42,
  provenance: null,
};

function wfJob(overrides: Partial<ResearchJob> = {}): ResearchJob {
  return {
    task_id: 'wf-1',
    status: 'running',
    progress: null,
    created_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
    finished_at: null,
    strategy_id: 'saw',
    name: 'Saw run',
    error: null,
    result_id: null,
    completed_trials: null,
    total_trials: null,
    ...overrides,
  };
}

function notFound(kind: 'ho' | 'wf') {
  return new ApiError(
    404,
    JSON.stringify({
      detail: {
        code: 'task_not_found',
        message: `${kind === 'ho' ? 'Hyperopt' : 'Walk-forward'} task ${kind}-1 not found`,
        state: 'not_found',
      },
    }),
  );
}

function notCompleted(status: string, error: string | null) {
  const base = `Walk-forward task wf-1 is not completed (status: ${status})`;
  return new ApiError(409, JSON.stringify({ detail: error ? `${base}: ${error}` : base }));
}

type Handler = (path: string, init?: RequestInit) => unknown;

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

function serve(overrides: Record<string, Handler | unknown> = {}) {
  fetchApi.mockImplementation(async (path: string, init?: RequestInit) => {
    calls.push({ path, init });
    const key = `${init?.method ?? 'GET'} ${path}`;
    const entry =
      overrides[key] ??
      {
        'GET /api/backtest/hyperopt/ho-1': () => HYPEROPT,
        'GET /api/backtest/hyperopt/ho-1/results': () => hyperoptResult(),
      }[key];
    const value = typeof entry === 'function' ? entry(path, init) : entry;
    if (value === undefined) throw new ApiError(404, JSON.stringify({ detail: 'no route' }));
    if (value instanceof Error) throw value;
    return value;
  });
}

beforeEach(() => {
  calls = [];
  params.taskId = 'ho-1';
  fetchApi.mockReset();
  router.push.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
  vi.useRealTimers();
});

describe('OptimizeTaskPage hyperopt', () => {
  it('renders the completed readouts, holdout, validation line and provenance', async () => {
    serve({
      'GET /api/backtest/hyperopt/ho-1/results': () =>
        hyperoptResult({
          provenance: {
            exchange: 'binance',
            symbol: 'BTCUSDT',
            timeframe: '1h',
            bar_count: 300,
            first_ts: '2025-01-01T00:00:00+00:00',
            last_ts: '2026-04-01T00:00:00+00:00',
            gap_count: 0,
            data_source: 'db',
            hydra_version: '2.4.0',
            config_hash: 'aabbccddeeff0011',
          },
        }),
    });
    page = await mount(<OptimizeTaskPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Best Sharpe 1.25');
    expect(text).toContain('Best params sma_period=42');
    expect(text).toContain('12 completed · 2 failed');
    expect(text).toContain('Seed 42');
    expect(text).toContain('Train window: 1 Jan 2025 to 1 Mar 2026');
    expect(text).toContain('Holdout window: 1 Mar 2026 to 1 Apr 2026');
    expect(text).toContain('Sharpe 0.90');
    expect(text).toContain('PBO 0.30 · DSR 0.41 · Holdout PSR 0.62');
    const panel = page.container.querySelector('[data-testid="provenance-panel"]');
    expect(panel?.textContent).toContain('Hydra 2.4.0');
    expect(panel?.textContent).toContain('Config aabbccddeeff…');
  });

  it('hides every holdout and validation field when the API returned none', async () => {
    serve({
      'GET /api/backtest/hyperopt/ho-1/results': () =>
        hyperoptResult({
          holdout: null,
          holdout_start: null,
          holdout_end: null,
          holdout_psr: null,
          dsr: null,
          pbo: null,
          train_start: null,
          train_end: null,
        }),
    });
    page = await mount(<OptimizeTaskPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Best Sharpe 1.25');
    expect(text).not.toContain('Holdout');
    expect(text).not.toContain('Holdout Sharpe');
    expect(text).not.toContain('DSR');
    expect(text).not.toContain('PBO');
    expect(text).not.toContain('Train window');
  });

  it('labels a failed trial with its error and never shows 0.00 for it', async () => {
    serve();
    page = await mount(<OptimizeTaskPage />);

    const rows = Array.from(page.container.querySelectorAll('tbody tr')).map((r) => r.textContent ?? '');
    expect(rows).toHaveLength(3);
    expect(rows[0]).toContain('1.23');
    expect(rows[1]).toContain('Failed: non-finite Sharpe ratio: nan');
    expect(rows[1]).not.toContain('0.00');
    expect(rows[2]).toContain('No trades');
    expect(rows[2]).not.toContain('0.00');
  });

  it('opens the builder with the strategy behind the best params', async () => {
    serve();
    page = await mount(<OptimizeTaskPage />);

    await click(buttonByText(page.container, 'Open in Builder'));
    expect(router.push).toHaveBeenCalledWith('/builder?strategy=saw');
  });

  it('polls an active run with a cancel, and shows why it failed', async () => {
    serve({
      'GET /api/backtest/hyperopt/ho-1': () => ({
        ...HYPEROPT,
        status: 'failed',
        error: 'no parameter set traded in-sample',
      }),
    });
    page = await mount(<OptimizeTaskPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Failed');
    expect(text).toContain('no parameter set traded in-sample');
    page.unmount();

    serve({ 'GET /api/backtest/hyperopt/ho-1': () => ({ ...HYPEROPT, status: 'running', completed_trials: 3 }) });
    page = await mount(<OptimizeTaskPage />);
    expect(buttonByText(page.container, 'Cancel')).not.toBeNull();
    expect(page.container.textContent).toContain('3/14 trials');
  });

  it('separates an unknown run from an outage', async () => {
    serve({ 'GET /api/backtest/hyperopt/ho-1': () => notFound('ho') });
    page = await mount(<OptimizeTaskPage />);
    expect(page.container.textContent).toContain('Run not found');
    page.unmount();

    serve({ 'GET /api/backtest/hyperopt/ho-1': () => unavailable() });
    page = await mount(<OptimizeTaskPage />);
    expect(page.container.textContent).toContain('Database is not available');
  });

  it('reads only the hyperopt endpoints for an ho- id', async () => {
    serve();
    page = await mount(<OptimizeTaskPage />);

    expect(calls.some((c) => c.path.startsWith('/api/backtest/hyperopt/ho-1'))).toBe(true);
    expect(calls.some((c) => c.path.startsWith('/api/backtest/jobs'))).toBe(false);
    expect(calls.some((c) => c.path.startsWith('/api/backtest/walkforward'))).toBe(false);
  });

  it('re-arms the 2s poll when Retry recovers contact with the run', async () => {
    vi.useFakeTimers();
    serve({
      'GET /api/backtest/hyperopt/ho-1': () => ({ ...HYPEROPT, status: 'running', completed_trials: 3 }),
    });
    page = await mount(<OptimizeTaskPage />);
    expect(page.container.textContent).toContain('3/14 trials');

    serve({ 'GET /api/backtest/hyperopt/ho-1': () => unavailable() });
    await advance(2000);
    expect(page.container.textContent).toContain('Database is not available');

    serve({
      'GET /api/backtest/hyperopt/ho-1': () => ({ ...HYPEROPT, status: 'running', completed_trials: 4 }),
    });
    await click(buttonByText(page.container, 'Retry'));
    expect(page.container.textContent).toContain('4/14 trials');

    const polls = () => calls.filter((c) => c.path === '/api/backtest/hyperopt/ho-1').length;
    const afterRetry = polls();
    await advance(2000);
    expect(polls()).toBe(afterRetry + 1);
  });
});

describe('OptimizeTaskPage walk-forward', () => {
  beforeEach(() => {
    params.taskId = 'wf-1';
  });

  it('renders the folds, readouts and stitched equity of a completed run', async () => {
    serve({
      'GET /api/backtest/jobs?kind=walkforward&limit=50': () => [
        wfJob({ status: 'completed', finished_at: new Date().toISOString() }),
      ],
      'GET /api/backtest/walkforward/wf-1/results': () => WF_RESULT,
    });
    page = await mount(<OptimizeTaskPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Walk-forward run');
    expect(text).toContain('OOS Sharpe 0.90');
    expect(text).toContain('OOS PSR 0.55');
    expect(text).toContain('Efficiency 0.75');
    expect(text).toContain('Anchored windows');
    expect(text).toContain('Seed 42');
    expect(text).toContain('1 Jan 2026 to 1 Mar 2026');
    expect(text).toContain('period=42');
    expect(page.container.querySelector('[role="img"]')?.getAttribute('aria-label')).toContain(
      'stitched across 1 folds',
    );
    expect(calls.some((c) => c.path.startsWith('/api/backtest/jobs'))).toBe(true);
    expect(calls.some((c) => c.path === '/api/backtest/walkforward/wf-1/results')).toBe(true);
  });

  it('shows the status panel with cancel while the run is not completed', async () => {
    serve({
      'GET /api/backtest/jobs?kind=walkforward&limit=50': () => [wfJob()],
      'GET /api/backtest/walkforward/wf-1/results': () => notCompleted('running', null),
    });
    page = await mount(<OptimizeTaskPage />);

    expect(buttonByText(page.container, 'Cancel')).not.toBeNull();
    expect(page.container.textContent).toContain(
      'Walk-forward task wf-1 is not completed (status: running)',
    );
  });

  it('renders a failed fold error verbatim', async () => {
    serve({
      'GET /api/backtest/jobs?kind=walkforward&limit=50': () => [
        wfJob({
          status: 'failed',
          error: 'fold 1: no parameter set traded in-sample',
          finished_at: new Date().toISOString(),
        }),
      ],
      'GET /api/backtest/walkforward/wf-1/results': () =>
        notCompleted('failed', 'fold 1: no parameter set traded in-sample'),
    });
    page = await mount(<OptimizeTaskPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Failed');
    expect(text).toContain('fold 1: no parameter set traded in-sample');
  });

  it('separates an unknown run from an outage', async () => {
    serve({
      'GET /api/backtest/jobs?kind=walkforward&limit=50': () => unavailable(),
      'GET /api/backtest/walkforward/wf-1/results': () => notFound('wf'),
    });
    page = await mount(<OptimizeTaskPage />);
    expect(page.container.textContent).toContain('Run not found');
    page.unmount();

    serve({
      'GET /api/backtest/jobs?kind=walkforward&limit=50': () => unavailable(),
      'GET /api/backtest/walkforward/wf-1/results': () => unavailable(),
    });
    page = await mount(<OptimizeTaskPage />);
    expect(page.container.textContent).toContain('Database is not available');
  });

  it('watches a run pushed out of the jobs window through the results endpoint', async () => {
    vi.useFakeTimers();
    serve({
      'GET /api/backtest/jobs?kind=walkforward&limit=50': () => [] as unknown[],
      'GET /api/backtest/walkforward/wf-1/results': () => notCompleted('running', null),
    });
    page = await mount(<OptimizeTaskPage />);

    const text = page.container.textContent ?? '';
    expect(text).not.toContain('Reading the run…');
    expect(text).toContain('Running');
    expect(buttonByText(page.container, 'Cancel')).not.toBeNull();

    serve({
      'GET /api/backtest/jobs?kind=walkforward&limit=50': () => [] as unknown[],
      'GET /api/backtest/walkforward/wf-1/results': () => WF_RESULT,
    });
    await advance(10000);

    expect(page.container.textContent).toContain('OOS Sharpe 0.90');
  });
});

describe('OptimizeTaskPage routing', () => {
  it('says Run not found for an id that is neither ho- nor wf-', async () => {
    params.taskId = 'task-1';
    serve();
    page = await mount(<OptimizeTaskPage />);

    expect(page.container.textContent).toContain('Run not found');
    expect(calls).toHaveLength(0);
  });
});
