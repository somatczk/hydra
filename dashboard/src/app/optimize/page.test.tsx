import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import type { ResearchJob } from '@/lib/research';
import {
  advance,
  buttonByText,
  click,
  flush,
  mount,
  sectionText,
  unavailable,
  type Mounted,
} from '@/test/pageHarness';

const fetchApi = vi.fn();
const toast = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast }) }));
const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => router }));

const { default: OptimizePage } = await import('./page');

const STRATEGIES = [{ id: 'saw', name: 'Saw', primary_timeframe: '1h' }];
const PARAMS = [
  { name: 'sma_period', label: 'SMA Period', type: 'int', default: 5, min: 2, max: 20, step: 1 },
];
const OTHER_PARAMS = [
  { name: 'rsi_period', label: 'RSI Period', type: 'int', default: 14, min: 5, max: 30, step: 1 },
];

/** Controllable promise: the test decides when a route's response lands. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const NO_DATA = new ApiError(
  422,
  JSON.stringify({
    detail: {
      code: 'no_market_data',
      message:
        'No market data for binance BTCUSDT 1h between 2025-01-01T00:00:00+00:00 and 2026-03-01T23:59:59.999999+00:00; available: 2026-09-09T00:00:00+00:00 to 2026-10-21T00:00:00+00:00',
      state: 'no_data',
      symbol: 'BTCUSDT',
      timeframe: '1h',
      exchange: 'binance',
      available_first_ts: '2026-09-09T00:00:00+00:00',
      available_last_ts: '2026-10-21T00:00:00+00:00',
    },
  }),
);

const RANGE_TOO_SHORT = new ApiError(
  422,
  JSON.stringify({
    detail: {
      code: 'range_too_short',
      message:
        'range too short for 6 folds: stored bars span 2026-09-09T00:00:00+00:00 to 2026-10-21T00:00:00+00:00; 6 out-of-sample windows and one in-sample window at train_pct 0.75 of at least 30 days each need about 270 days',
      state: 'invalid',
      available_first_ts: '2026-09-09T00:00:00+00:00',
      available_last_ts: '2026-10-21T00:00:00+00:00',
    },
  }),
);

/** A durable job row, aged relative to now so the rendered age text stays stable. */
function job(overrides: Partial<ResearchJob & { kind: string }> = {}): ResearchJob & { kind: string } {
  return {
    task_id: 'ho-1',
    kind: 'hyperopt',
    status: 'running',
    progress: null,
    created_at: new Date(Date.now() - 3 * 3600_000).toISOString(),
    finished_at: null,
    strategy_id: 'saw',
    name: 'Saw run',
    error: null,
    result_id: null,
    completed_trials: 3,
    total_trials: 10,
    ...overrides,
  };
}

function runningProgress() {
  return {
    task_id: 'ho-1',
    status: 'running',
    strategy_id: 'saw',
    created_at: new Date(Date.now() - 60_000).toISOString(),
    completed_trials: 3,
    total_trials: 10,
    error: null,
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
    {
      fold_index: 1,
      in_sample_sharpe: 1.1,
      in_sample_return: 0.06,
      in_sample_drawdown: 0.02,
      in_sample_trades: 10,
      out_of_sample_sharpe: 0.7,
      out_of_sample_return: 0.02,
      out_of_sample_drawdown: 0.04,
      out_of_sample_trades: 7,
      selected_params: { period: 30 },
      n_trials: 7,
      is_start: '2025-12-01T00:00:00+00:00',
      is_end: '2026-02-01T00:00:00+00:00',
      oos_start: '2026-04-01T00:00:00+00:00',
      oos_end: '2026-05-01T00:00:00+00:00',
    },
  ],
  aggregated_sharpe: 0.8,
  aggregated_return: 0.06,
  aggregated_drawdown: 0.07,
  out_of_sample_equity: [10000, 10100, 10200, 10300],
  oos_psr: null,
  efficiency: null,
  anchored: false,
  seed: 42,
  provenance: null,
};

type Handler = (path: string, init?: RequestInit) => unknown;

let page: Mounted | null = null;
let posts: Array<Record<string, unknown>>;
let calls: Array<{ path: string; init?: RequestInit }>;

function serve(overrides: Record<string, Handler | unknown> = {}) {
  fetchApi.mockImplementation(async (path: string, init?: RequestInit) => {
    calls.push({ path, init });
    const key = `${init?.method ?? 'GET'} ${path}`;
    if (init?.method === 'POST' && init.body) posts.push(JSON.parse(String(init.body)));
    const entry =
      overrides[key] ??
      {
        'GET /api/backtest/strategies': () => STRATEGIES,
        [`GET /api/backtest/strategies/${STRATEGIES[0].id}/params`]: () => PARAMS,
        'GET /api/backtest/jobs?kind=hyperopt&limit=50': () => [] as unknown[],
        'GET /api/backtest/jobs?kind=walkforward&limit=50': () => [] as unknown[],
      }[key];
    const value = typeof entry === 'function' ? entry(path, init) : entry;
    if (value === undefined) throw new ApiError(404, JSON.stringify({ detail: 'no route' }));
    if (value instanceof Error) throw value;
    return value;
  });
}

beforeEach(() => {
  posts = [];
  calls = [];
  fetchApi.mockReset();
  toast.mockReset();
  router.push.mockReset();
  localStorage.clear();
});

afterEach(() => {
  page?.unmount();
  page = null;
  vi.useRealTimers();
});

const labels = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('label')).map((l) => l.textContent);

describe('OptimizePage segmented control', () => {
  it('exposes a labelled group and switches the per-mode fields', async () => {
    serve();
    page = await mount(<OptimizePage />);

    const group = page.container.querySelector('div[role="group"][aria-label="Optimisation mode"]');
    expect(group).not.toBeNull();
    const [ho, wf] = Array.from(group!.querySelectorAll('button'));
    expect(ho.textContent).toBe('Hyperopt');
    expect(wf.textContent).toBe('Walk-forward');
    expect(ho.getAttribute('aria-pressed')).toBe('true');
    expect(wf.getAttribute('aria-pressed')).toBe('false');

    expect(labels(page.container)).toContain('Max Trials');
    expect(labels(page.container)).not.toContain('Folds');
    expect(labels(page.container)).toContain('Initial capital (USDT)');
    expect(page.container.querySelector('select option[value=""]')?.textContent).toBe(
      'Select strategy…',
    );

    await click(wf);
    expect(ho.getAttribute('aria-pressed')).toBe('false');
    expect(wf.getAttribute('aria-pressed')).toBe('true');
    expect(labels(page.container)).not.toContain('Max Trials');
    for (const label of ['Folds', 'Train %', 'Trials per fold', 'Anchored']) {
      expect(labels(page.container)).toContain(label);
    }
    // fold_count is the fold count: the default 424 days at 6 and 75 % tile as 142 + 6 × 47 days
    expect(page.container.textContent).toContain(
      '6 test windows of ~47 days after ~142 days of training, rolled by the test window',
    );
  });

  it('has one h1 and the phone Research nav with Optimize current', async () => {
    serve();
    page = await mount(<OptimizePage />);

    expect(page.container.querySelectorAll('h1')).toHaveLength(1);
    expect(page.container.querySelector('h1')?.textContent).toBe('Strategy optimiser');
    expect(
      page.container.querySelector('nav[aria-label="Research"] [aria-current="page"]')?.textContent,
    ).toBe('Optimize');
  });
});

describe('OptimizePage submissions', () => {
  it('sends the exact hyperopt body, params included', async () => {
    serve({ 'POST /api/backtest/hyperopt': () => ({ task_id: 'ho-1', status: 'queued' }) });
    page = await mount(<OptimizePage />);

    await click(buttonByText(page.container, 'Run Hyperopt'));

    expect(posts).toHaveLength(1);
    expect(posts[0]).toEqual({
      strategy_id: 'saw',
      param_space: [{ name: 'sma_period', type: 'int', low: 2, high: 20 }],
      method: 'bayesian',
      max_trials: 100,
      start_date: '2025-01-01',
      end_date: '2026-03-01',
      initial_capital: 10000,
    });
  });

  it('sends the exact walk-forward body, train_pct as a fraction', async () => {
    serve({ 'POST /api/backtest/walkforward': () => ({ task_id: 'wf-1', status: 'queued' }) });
    page = await mount(<OptimizePage />);

    await click(buttonByText(page.container, 'Walk-forward'));
    await click(page.container.querySelector('input[type="checkbox"]')!);
    await click(buttonByText(page.container, 'Run Walk-forward'));

    expect(posts).toHaveLength(1);
    expect(posts[0]).toEqual({
      strategy_id: 'saw',
      param_space: [{ name: 'sma_period', type: 'int', low: 2, high: 20 }],
      method: 'bayesian',
      max_trials: 20,
      fold_count: 6,
      train_pct: 0.75,
      anchored: true,
      start_date: '2025-01-01',
      end_date: '2026-03-01',
      initial_capital: 10000,
    });
  });

  it('cannot submit without a param space', async () => {
    serve({
      [`GET /api/backtest/strategies/${STRATEGIES[0].id}/params`]: () => [],
      'POST /api/backtest/walkforward': () => ({ task_id: 'wf-1', status: 'queued' }),
    });
    page = await mount(<OptimizePage />);

    expect(page.container.textContent).toContain('This strategy has no optimisable parameters.');
    expect(buttonByText(page.container, 'Run Hyperopt').disabled).toBe(true);
    await click(buttonByText(page.container, 'Walk-forward'));
    expect(buttonByText(page.container, 'Run Walk-forward').disabled).toBe(true);

    expect(posts).toHaveLength(0);
  });

  it('shows the no-stored-bars sentence with the coverage line for a 422', async () => {
    serve({ 'POST /api/backtest/hyperopt': () => NO_DATA });
    page = await mount(<OptimizePage />);

    await click(buttonByText(page.container, 'Run Hyperopt'));

    const card = page.container.querySelector('[data-testid="run-error"]');
    expect(card?.textContent).toContain(
      'No stored bars for BTCUSDT 1h in this range. Nothing was simulated.',
    );
    expect(card?.textContent).toContain('Stored bars cover 9 Sep 2026 to 21 Oct 2026.');
    expect(card?.querySelector('[data-state="no_data"]')?.textContent).toBe('NO DATA');
  });

  it('renders the frozen range_too_short sentence with date-only coverage', async () => {
    serve({ 'POST /api/backtest/walkforward': () => RANGE_TOO_SHORT });
    page = await mount(<OptimizePage />);

    await click(buttonByText(page.container, 'Walk-forward'));
    await click(buttonByText(page.container, 'Run Walk-forward'));

    const card = page.container.querySelector('[data-testid="run-error"]');
    expect(card?.textContent).toContain('The stored range is too short for 6 folds.');
    expect(card?.textContent).toContain('Stored bars cover 9 Sep 2026 to 21 Oct 2026.');
    expect(card?.querySelector('[data-state="invalid"]')?.textContent).toBe('INVALID');
    expect(card?.textContent).not.toContain('{');
  });

  it('clears the parameter space when the next strategy params read fails', async () => {
    serve({
      'GET /api/backtest/strategies': () => [...STRATEGIES, { id: 'other', name: 'Other' }],
      'GET /api/backtest/strategies/other/params': () =>
        new ApiError(
          404,
          JSON.stringify({
            detail: {
              code: 'strategy_not_found',
              message: 'Strategy other not found',
              state: 'not_found',
            },
          }),
        ),
    });
    page = await mount(<OptimizePage />);

    expect(buttonByText(page.container, 'Run Hyperopt').disabled).toBe(false);

    const label = Array.from(page.container.querySelectorAll('label')).find(
      (l) => l.textContent === 'Strategy',
    )!;
    const select = document.getElementById(label.htmlFor) as HTMLSelectElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
    await act(async () => {
      setter?.call(select, 'other');
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });

    expect(page.container.textContent).toContain('Strategy other not found');
    expect(buttonByText(page.container, 'Run Hyperopt').disabled).toBe(true);
  });

  it('reports the server reason when the strategy params read fails', async () => {
    serve({
      [`GET /api/backtest/strategies/${STRATEGIES[0].id}/params`]: () =>
        new ApiError(
          404,
          JSON.stringify({
            detail: { code: 'strategy_not_found', message: 'Strategy saw not found', state: 'not_found' },
          }),
        ),
    });
    page = await mount(<OptimizePage />);

    expect(page.container.textContent).toContain('Strategy saw not found');
    expect(page.container.textContent).not.toContain('This strategy has no optimisable parameters.');
    expect(buttonByText(page.container, 'Run Hyperopt').disabled).toBe(true);
  });

  it('binds the param space to the current strategy when params responses land out of order', async () => {
    const sawParams = deferred<unknown[]>();
    serve({
      'GET /api/backtest/strategies': () => [...STRATEGIES, { id: 'other', name: 'Other' }],
      'GET /api/backtest/strategies/saw/params': () => sawParams.promise,
      'GET /api/backtest/strategies/other/params': () => OTHER_PARAMS,
    });
    page = await mount(<OptimizePage />);

    const label = Array.from(page.container.querySelectorAll('label')).find(
      (l) => l.textContent === 'Strategy',
    )!;
    const select = document.getElementById(label.htmlFor) as HTMLSelectElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
    await act(async () => {
      setter?.call(select, 'other');
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await flush();

    expect(page.container.textContent).toContain('RSI Period');

    sawParams.resolve(PARAMS); // the previous strategy's response lands last
    await flush();

    expect(page.container.textContent).toContain('RSI Period');
    expect(page.container.textContent).not.toContain('SMA Period');
  });
});

describe('OptimizePage hyperopt run', () => {
  it('watches the started run with cancel, then navigates to its results', async () => {
    vi.useFakeTimers();
    let status = 'running';
    serve({
      'POST /api/backtest/hyperopt': () => ({ task_id: 'ho-1', status: 'queued' }),
      'GET /api/backtest/hyperopt/ho-1': () => ({ ...runningProgress(), status }),
    });
    page = await mount(<OptimizePage />);

    await click(buttonByText(page.container, 'Run Hyperopt'));

    expect(page.container.textContent).toContain('Running');
    expect(page.container.textContent).toContain('3/10 trials');
    expect(buttonByText(page.container, 'Cancel')).not.toBeNull();
    expect(localStorage.getItem('hyperopt_task_id')).toBeNull();

    status = 'completed';
    await advance(2000);

    expect(router.push).toHaveBeenCalledWith('/optimize/ho-1');
    expect(page.container.querySelector('a[href="/optimize/ho-1"]')?.textContent).toBe('View results');
  });

  it('never asks the retired hyperopt history endpoint', async () => {
    serve();
    page = await mount(<OptimizePage />);

    expect(calls.some((c) => c.path.includes('/hyperopt/history'))).toBe(false);
  });

  it('never navigates when the completing poll lands after unmount', async () => {
    vi.useFakeTimers();
    let land: ((value: unknown) => void) | null = null;
    serve({
      'POST /api/backtest/hyperopt': () => ({ task_id: 'ho-1', status: 'queued' }),
      'GET /api/backtest/hyperopt/ho-1': () =>
        new Promise((resolve) => {
          land = resolve;
        }),
    });
    page = await mount(<OptimizePage />);
    await click(buttonByText(page.container, 'Run Hyperopt'));

    page.unmount();
    page = null;
    await act(async () => {
      land?.({ ...runningProgress(), status: 'completed' });
    });

    expect(router.push).not.toHaveBeenCalled();
  });

  it('re-arms the watcher from the submitted id when the very first poll failed', async () => {
    vi.useFakeTimers();
    serve({
      'POST /api/backtest/hyperopt': () => ({ task_id: 'ho-1', status: 'queued' }),
      'GET /api/backtest/hyperopt/ho-1': () => unavailable(),
    });
    page = await mount(<OptimizePage />);
    await click(buttonByText(page.container, 'Run Hyperopt'));

    expect(page.container.textContent).toContain('Database is not available');
    expect(page.container.textContent).not.toContain('Running');

    serve({
      'POST /api/backtest/hyperopt': () => ({ task_id: 'ho-1', status: 'queued' }),
      'GET /api/backtest/hyperopt/ho-1': () => runningProgress(),
    });
    await click(buttonByText(page.container, 'Retry'));

    const polls = () => calls.filter((c) => c.path === '/api/backtest/hyperopt/ho-1').length;
    const afterRetry = polls();
    await advance(2000);
    expect(polls()).toBe(afterRetry + 1);
    expect(page.container.textContent).toContain('Running');
  });
});

describe('OptimizePage walk-forward run', () => {
  it('renders the folds inline once the durable row completes, hiding absent fields', async () => {
    vi.useFakeTimers();
    let status = 'running';
    serve({
      'POST /api/backtest/walkforward': () => ({ task_id: 'wf-1', status: 'queued' }),
      'GET /api/backtest/jobs?kind=walkforward&limit=50': () => [
        job({
          kind: 'walkforward',
          task_id: 'wf-1',
          status,
          completed_trials: null,
          total_trials: null,
        }),
      ],
      'GET /api/backtest/walkforward/wf-1/results': () => WF_RESULT,
    });
    page = await mount(<OptimizePage />);

    await click(buttonByText(page.container, 'Walk-forward'));
    await click(buttonByText(page.container, 'Run Walk-forward'));

    expect(sectionText(page.container, 'Recent runs')).toContain('Running');

    status = 'completed';
    await advance(10000);

    const text = page.container.textContent ?? '';
    expect(text).toContain('1 Jan 2026 to 1 Mar 2026');
    expect(text).toContain('1 Mar 2026 to 31 Mar 2026');
    expect(text).toContain('period=42');
    expect(text).toContain('OOS Sharpe 0.80');
    expect(text).not.toContain('OOS PSR');
    expect(text).not.toContain('Efficiency');
    const chart = page.container.querySelector('[role="img"]');
    expect(chart?.getAttribute('aria-label')).toContain('stitched across 2 folds');
  });
});

describe('OptimizePage recent runs', () => {
  it('lists both kinds with ages, links and verbatim errors, from the jobs route', async () => {
    serve({
      'GET /api/backtest/jobs?kind=hyperopt&limit=50': () => [
        job({
          task_id: 'ho-9',
          status: 'completed',
          finished_at: new Date(Date.now() - 3600_000).toISOString(),
        }),
      ],
      'GET /api/backtest/jobs?kind=walkforward&limit=50': () => [
        job({
          task_id: 'wf-3',
          kind: 'walkforward',
          status: 'failed',
          completed_trials: null,
          total_trials: null,
          finished_at: new Date(Date.now() - 3600_000).toISOString(),
          error: 'fold 1: no parameter set traded in-sample',
        }),
      ],
    });
    page = await mount(<OptimizePage />);

    const section = sectionText(page.container, 'Recent runs');
    expect(section).toContain('Finished 1 h ago');
    expect(section).toContain('fold 1: no parameter set traded in-sample');
    expect(page.container.querySelector('a[href="/optimize/ho-9"]')?.textContent).toBe('View results');
    expect(calls.some((c) => c.path.includes('/hyperopt/history'))).toBe(false);
  });

  it('reports an unavailable jobs list instead of an empty list', async () => {
    serve({
      'GET /api/backtest/jobs?kind=hyperopt&limit=50': () => unavailable(),
      'GET /api/backtest/jobs?kind=walkforward&limit=50': () => unavailable(),
    });
    page = await mount(<OptimizePage />);

    expect(sectionText(page.container, 'Recent runs')).toContain('Database is not available');
  });

  it('keeps the runs across poll ticks with no lost-on-restart copy', async () => {
    vi.useFakeTimers();
    serve({
      'GET /api/backtest/jobs?kind=hyperopt&limit=50': () => [job()],
    });
    page = await mount(<OptimizePage />);

    await advance(10000);
    await advance(10000);

    expect(page.container.textContent).toContain('3/10 trials');
    expect(page.container.textContent).toContain('Started 3 h ago');
    expect(page.container.textContent).not.toContain('may have restarted');
    expect(page.container.textContent).not.toContain('no longer known');
    expect(page.container.textContent).not.toContain('lost');
  });
});
