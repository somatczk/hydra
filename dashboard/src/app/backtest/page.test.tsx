import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import type { ResearchJob } from '@/lib/research';
import {
  advance,
  buttonByText,
  choose,
  click,
  dialogPolyfill,
  flush,
  mount,
  sectionText,
  selectByLabel,
  unavailable,
  type Mounted,
} from '@/test/pageHarness';

const fetchApi = vi.fn();
const toast = vi.fn();
const nav = vi.hoisted(() => ({ search: new URLSearchParams() }));
const router = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast }) }));
vi.mock('next/navigation', () => ({
  useRouter: () => router,
  useSearchParams: () => nav.search,
}));

const { default: BacktestPage } = await import('./page');

const NO_DATA = new ApiError(
  422,
  JSON.stringify({
    detail: {
      code: 'no_market_data',
      message:
        'No market data for binance BTCUSDT 4h between 2026-01-01T00:00:00+00:00 and 2026-03-01T23:59:59.999999+00:00; available: 2026-09-09T00:00:00+00:00 to 2026-10-21T00:00:00+00:00',
      state: 'no_data',
      symbol: 'BTCUSDT',
      timeframe: '4h',
      exchange: 'binance',
      available_first_ts: '2026-09-09T00:00:00+00:00',
      available_last_ts: '2026-10-21T00:00:00+00:00',
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

const RESULT2 = { ...RESULT, id: 'bt-task-2', name: 'second' };
const RESULT3 = { ...RESULT, id: 'bt-task-3', name: 'third' };

type Handler = (path: string, init?: RequestInit) => unknown;

let page: Mounted | null = null;
let posts: Array<Record<string, unknown>>;
let calls: Array<{ path: string; init?: RequestInit }>;

/** A durable job row, aged relative to now so the rendered age text stays stable. */
function job(overrides: Partial<ResearchJob> = {}): ResearchJob {
  return {
    task_id: 'task-1',
    status: 'running',
    progress: 42,
    created_at: new Date(Date.now() - 3 * 3600_000).toISOString(),
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

function serve(overrides: Record<string, Handler | unknown> = {}) {
  fetchApi.mockImplementation(async (path: string, init?: RequestInit) => {
    calls.push({ path, init });
    const key = `${init?.method ?? 'GET'} ${path.split('?')[0]}`;
    if (init?.method === 'POST' && init.body) posts.push(JSON.parse(String(init.body)));
    const entry =
      overrides[key] ??
      {
        'GET /api/backtest/results': [RESULT],
        'GET /api/backtest/strategies': [{ id: 'saw', name: 'Saw', primary_timeframe: '4h' }],
        'GET /api/backtest/jobs': [],
      }[key];
    const value = typeof entry === 'function' ? entry(path, init) : entry;
    if (value === undefined) throw new ApiError(404, JSON.stringify({ detail: 'no route' }));
    if (value instanceof Error) throw value;
    return value;
  });
}

beforeEach(() => {
  nav.search = new URLSearchParams();
  posts = [];
  calls = [];
  fetchApi.mockReset();
  toast.mockReset();
  router.push.mockReset();
  localStorage.clear();
  dialogPolyfill();
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

  it('offers the model pin only for a strategy that serves a model, and sends it only when chosen', async () => {
    serve({
      'GET /api/backtest/strategies': [
        { id: 'saw', name: 'Saw', primary_timeframe: '4h', model_names: [] },
        { id: 'native', name: 'Native', primary_timeframe: '1h', model_names: ['alpha'] },
      ],
      'GET /api/models': [
        { id: 'a1', name: 'alpha', version: 1, stage: 'Production' },
        { id: 'a2', name: 'alpha', version: 2, stage: 'Staging' },
      ],
      'POST /api/backtest/run': { task_id: 'task-9' },
    });
    page = await mount(<BacktestPage />);
    expect(page.container.textContent).not.toContain('Model version');

    await choose(selectByLabel(page.container, 'Strategy'), 'native');
    await click(buttonByText(page.container, 'Run Backtest'));
    expect(posts[0]).toMatchObject({ strategy_id: 'native' });
    expect(posts[0]).not.toHaveProperty('model_version_id');

    await choose(selectByLabel(page.container, 'Model version'), 'a2');
    await click(buttonByText(page.container, 'Run Backtest'));
    expect(posts[1]).toMatchObject({ strategy_id: 'native', model_version_id: 'a2' });

    await choose(selectByLabel(page.container, 'Strategy'), 'saw');
    await choose(selectByLabel(page.container, 'Strategy'), 'native');
    expect(selectByLabel(page.container, 'Model version').value).toBe('');
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

  it('shows the no-stored-bars sentence with the coverage line, persistently', async () => {
    serve({ 'POST /api/backtest/run': () => NO_DATA });
    page = await mount(<BacktestPage />);

    await click(buttonByText(page.container, 'Run Backtest'));

    const card = page.container.querySelector('[data-testid="run-error"]');
    expect(card?.textContent).toContain(
      'No stored bars for BTCUSDT 4h in this range. Nothing was simulated.',
    );
    expect(card?.textContent).toContain('Stored bars cover 9 Sep 2026 to 21 Oct 2026.');
    expect(card?.querySelector('[data-state="no_data"]')?.textContent).toBe('NO DATA');
    expect(card?.textContent).not.toContain('{');
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

  it('labels a cancelled row Cancelled, never Running', async () => {
    serve({ 'GET /api/backtest/results': () => [{ ...RESULT, status: 'cancelled' }] });
    page = await mount(<BacktestPage />);

    const badges = Array.from(page.container.querySelectorAll('span')).map((s) => s.textContent ?? '');
    expect(badges).toContain('Cancelled');
    expect(badges).not.toContain('Running');
  });
});

describe('BacktestPage jobs', () => {
  it('renders every job state with ages, progress, verbatim error and links', async () => {
    serve({
      'GET /api/backtest/results': () => [],
      'GET /api/backtest/jobs': () => [
        job({ task_id: 'task-q', status: 'queued', progress: 0, name: 'queued run' }),
        job({ task_id: 'task-r', status: 'running', progress: 42, name: 'running run' }),
        job({
          task_id: 'task-c',
          status: 'completed',
          progress: 100,
          name: 'done run',
          finished_at: new Date(Date.now() - 3600_000).toISOString(),
          result_id: 'res-9',
        }),
        job({
          task_id: 'task-f',
          status: 'failed',
          name: 'failed run',
          error: 'worker died mid-run',
          finished_at: new Date(Date.now() - 3600_000).toISOString(),
        }),
        job({
          task_id: 'task-x',
          status: 'cancelled',
          name: 'cancelled run',
          finished_at: new Date(Date.now() - 3600_000).toISOString(),
        }),
      ],
    });
    page = await mount(<BacktestPage />);

    const badges = Array.from(page.container.querySelectorAll('span'))
      .filter((s) => s.children.length === 0)
      .map((s) => s.textContent ?? '')
      .filter((t) => ['Queued', 'Running', 'Completed', 'Failed', 'Cancelled'].includes(t));
    expect(badges).toEqual(['Queued', 'Running', 'Completed', 'Failed', 'Cancelled']);
    expect(page.container.textContent).toContain('Started 3 h ago');
    expect(page.container.textContent).toContain('42%');
    expect(page.container.textContent).toContain('worker died mid-run');
    expect(page.container.querySelector('a[href="/backtest/res-9"]')?.textContent).toBe('View result');
  });

  it('a 202 never touches localStorage and the job appears on the next jobs read', async () => {
    vi.useFakeTimers();
    let started = false;
    serve({
      'POST /api/backtest/run': () => {
        started = true;
        return { task_id: 'task-9', status: 'queued' };
      },
      'GET /api/backtest/jobs': () =>
        started ? [job({ task_id: 'task-9', status: 'queued', progress: 0, name: 'new run' })] : [],
    });
    page = await mount(<BacktestPage />);

    expect(page.container.textContent).not.toContain('new run');
    await click(buttonByText(page.container, 'Run Backtest'));

    expect(posts).toHaveLength(1);
    expect(localStorage.getItem('backtest_task_id')).toBeNull();
    expect(page.container.textContent).toContain('new run');
    expect(page.container.textContent).toContain('Queued');
  });

  it('keeps the job list across poll ticks, with no lost-task copy', async () => {
    vi.useFakeTimers();
    serve({ 'GET /api/backtest/jobs': () => [job({ status: 'running', progress: 42 })] });
    page = await mount(<BacktestPage />);

    await advance(10000);
    await advance(10000);

    expect(page.container.textContent).toContain('42%');
    expect(page.container.textContent).toContain('Started 3 h ago');
    expect(page.container.textContent).not.toContain('may have restarted');
    expect(page.container.textContent).not.toContain('no longer known');
  });

  it('reports an unavailable jobs list instead of an empty list', async () => {
    serve({ 'GET /api/backtest/jobs': () => unavailable() });
    page = await mount(<BacktestPage />);

    expect(sectionText(page.container, 'Jobs')).toContain('Database is not available');
  });

  it('says Failed to load jobs when the read fails without a server message', async () => {
    serve({ 'GET /api/backtest/jobs': () => new Error('network gone') });
    page = await mount(<BacktestPage />);

    const text = sectionText(page.container, 'Jobs');
    expect(text).toContain('Failed to load jobs');
    expect(text).toContain('Retry');
  });

  it('cancels a job through the confirm dialog exactly once', async () => {
    let cancelled = false;
    serve({
      'GET /api/backtest/jobs': () => [
        cancelled ? job({ status: 'cancelling' }) : job({ status: 'running', progress: 42 }),
      ],
      'POST /api/backtest/jobs/task-1/cancel': () => {
        cancelled = true;
        return { task_id: 'task-1', status: 'cancelling' };
      },
    });
    page = await mount(<BacktestPage />);

    await click(buttonByText(page.container, 'Cancel'));
    const dialog = page.container.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(document.activeElement?.textContent).toBe('Cancel this run?');

    await click(buttonByText(page.container, 'Cancel run'));
    const cancels = calls.filter((c) => c.path === '/api/backtest/jobs/task-1/cancel');
    expect(cancels).toHaveLength(1);
    expect(page.container.textContent).toContain('Cancelling…');
  });

  it('says the run already finished when the cancel hits a 409', async () => {
    serve({
      'GET /api/backtest/jobs': () => [job({ status: 'running' })],
      'POST /api/backtest/jobs/task-1/cancel': new ApiError(
        409,
        JSON.stringify({ detail: 'Task task-1 is already completed' }),
      ),
    });
    page = await mount(<BacktestPage />);

    await click(buttonByText(page.container, 'Cancel'));
    await click(buttonByText(page.container, 'Cancel run'));

    expect(page.container.textContent).toContain('This run already finished.');
  });
});

describe('BacktestPage result actions', () => {
  it('deletes a result through a destructive confirm focused on its title', async () => {
    serve({ 'DELETE /api/backtest/results/bt-task-1': () => null });
    page = await mount(<BacktestPage />);

    await click(page.container.querySelector('button[aria-label="Delete backtest real"]')!);
    const dialog = page.container.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(document.activeElement?.textContent).toBe('Delete this backtest?');
    expect(dialog.textContent).toContain('This cannot be undone.');

    await click(buttonByText(page.container, 'Delete backtest'));
    const deletes = calls.filter(
      (c) => c.path === '/api/backtest/results/bt-task-1' && c.init?.method === 'DELETE',
    );
    expect(deletes).toHaveLength(1);
    expect(page.container.textContent).not.toContain('real');
    expect(toast).toHaveBeenCalledWith('success', 'Backtest deleted');
  });

  it('compares two selected results and ignores a third selection', async () => {
    serve({ 'GET /api/backtest/results': () => [RESULT, RESULT2, RESULT3] });
    page = await mount(<BacktestPage />);

    const compare = buttonByText(page.container, 'Compare 2 selected');
    expect(compare.disabled).toBe(true);

    const boxes = page.container.querySelectorAll<HTMLInputElement>('tbody input[type="checkbox"]');
    expect(boxes).toHaveLength(3);
    await click(boxes[0]);
    await click(boxes[1]);
    expect(compare.disabled).toBe(false);

    await act(async () => {
      boxes[2].click();
    });
    expect(boxes[2].checked).toBe(false);

    await click(compare);
    expect(router.push).toHaveBeenCalledWith('/backtest/compare?a=bt-task-1&b=bt-task-2');
  });
});
