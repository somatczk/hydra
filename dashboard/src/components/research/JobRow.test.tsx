import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import {
  buttonByText,
  click,
  dialogPolyfill,
  flush,
  mount,
  routeFetch,
  type Mounted,
  type Mutations,
} from '@/test/pageHarness';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { JobRow } = await import('./JobRow');

const NOW = Date.parse('2026-10-25T12:00:00+00:00');

const RUNNING = {
  task_id: 'task-1',
  status: 'running',
  progress: 40,
  created_at: '2026-10-25T09:00:00+00:00',
  finished_at: null,
  strategy_id: 'saw',
  name: 'Saw run',
  error: null,
  result_id: null,
};

let m: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;
let refetches: number;

const refetch = () => {
  refetches += 1;
};

const posts = () =>
  calls.filter((c) => c.path === '/api/backtest/jobs/task-1/cancel' && c.init?.method === 'POST');

const cancelTriggers = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('button')).filter((b) => b.textContent?.trim() === 'Cancel');

function setup(mutations: Mutations = {}) {
  fetchApi.mockImplementation(routeFetch({}, calls, mutations));
}

beforeEach(() => {
  dialogPolyfill();
  calls = [];
  refetches = 0;
});

afterEach(() => {
  m?.unmount();
  m = null;
  fetchApi.mockReset();
});

describe('JobRow', () => {
  it('renders status text, age and progress for a running job', async () => {
    m = await mount(<JobRow kind="backtest" job={RUNNING} now={NOW} />);
    const text = m.container.textContent ?? '';
    expect(text).toContain('Running');
    expect(text).toContain('40%');
    expect(text).toContain('Started 3 h ago');
    expect(cancelTriggers(m.container)).toHaveLength(1);
  });

  it('shows the worker fetching missing bars while the job is in its fetch phase', async () => {
    const fetch = {
      state: 'fetching',
      ranges: [['2026-01-01T00:00:00+00:00', '2026-01-31T23:00:00+00:00']],
      bars_fetched: 300,
      weight_spent: 6,
      elapsed_ms: 4200,
    };
    m = await mount(<JobRow kind="walkforward" job={{ ...RUNNING, progress: 6, fetch }} now={NOW} />);
    expect(m.container.querySelector('[data-testid="job-fetch"]')?.textContent).toBe(
      'Fetching missing bars: 300 so far over 1 range (1 Jan 2026 – 31 Jan 2026) · weight 6 · 4 s',
    );
  });

  it('shows no fetch line for a job the worker never fetched for', async () => {
    m = await mount(<JobRow kind="backtest" job={{ ...RUNNING, fetch: null }} now={NOW} />);
    expect(m.container.querySelector('[data-testid="job-fetch"]')).toBeNull();
  });

  it('links a completed backtest to its result and offers no cancel', async () => {
    m = await mount(
      <JobRow
        kind="backtest"
        job={{ ...RUNNING, status: 'completed', progress: 100, finished_at: '2026-10-25T11:00:00+00:00', result_id: 'res-9' }}
        now={NOW}
      />,
    );
    expect(m.container.querySelector('a[href="/backtest/res-9"]')).toBeTruthy();
    expect(m.container.textContent).toContain('Finished 1 h ago');
    expect(cancelTriggers(m.container)).toHaveLength(0);
  });

  it('links a completed hyperopt to its results and shows the trial count', async () => {
    m = await mount(
      <JobRow
        kind="hyperopt"
        job={{ ...RUNNING, task_id: 'ho-7', status: 'completed', completed_trials: 100, total_trials: 100, finished_at: '2026-10-25T11:00:00+00:00' }}
        now={NOW}
      />,
    );
    expect(m.container.querySelector('a[href="/optimize/ho-7"]')).toBeTruthy();
    expect(m.container.textContent).toContain('100/100 trials');
  });

  it('renders Cancelled and Cancelling… without a cancel control', async () => {
    m = await mount(
      <JobRow kind="backtest" job={{ ...RUNNING, status: 'cancelled', finished_at: '2026-10-25T11:00:00+00:00' }} now={NOW} />,
    );
    expect(m.container.textContent).toContain('Cancelled');
    expect(cancelTriggers(m.container)).toHaveLength(0);

    m?.unmount();
    m = await mount(<JobRow kind="backtest" job={{ ...RUNNING, status: 'cancelling' }} now={NOW} />);
    expect(m.container.textContent).toContain('Cancelling…');
    expect(cancelTriggers(m.container)).toHaveLength(0);
  });

  it('renders a failed job without hiding it behind the result row', async () => {
    m = await mount(
      <JobRow kind="backtest" job={{ ...RUNNING, status: 'failed', error: 'worker died mid-run', finished_at: '2026-10-25T11:00:00+00:00' }} now={NOW} />,
    );
    const error = Array.from(m.container.querySelectorAll('p')).find(
      (p) => p.textContent === 'worker died mid-run',
    );
    expect(error).toBeTruthy();
    expect(error?.className).toContain('text-danger');
    expect(m.container.querySelector('a[href^="/backtest/"]')).toBeNull();
  });

  it('opens the cancel confirm focused on its title with the frozen facts', async () => {
    setup();
    m = await mount(<JobRow kind="backtest" job={RUNNING} now={NOW} />);
    await click(buttonByText(m.container, 'Cancel'));
    const dialog = m.container.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(document.activeElement?.textContent).toBe('Cancel this run?');
    expect(document.activeElement?.tagName).not.toBe('BUTTON');
    expect(dialog.textContent).toContain('The worker stops at the next checkpoint. Anything already finished stays.');
    expect(buttonByText(dialog, 'Cancel run')).toBeTruthy();
    expect(buttonByText(dialog, 'Keep running')).toBeTruthy();
  });

  it('keeps running without posting when the secondary is chosen', async () => {
    setup();
    m = await mount(<JobRow kind="backtest" job={RUNNING} now={NOW} onRefetch={refetch} />);
    await click(buttonByText(m.container, 'Cancel'));
    await click(buttonByText(m.container, 'Keep running'));
    expect(posts()).toHaveLength(0);
    expect(refetches).toBe(0);
    expect(m.container.querySelector('dialog[open]')).toBeNull();
  });

  it('posts the cancel exactly once on a double click, then refetches', async () => {
    let resolvePost: (v: unknown) => void = () => {};
    setup({
      'POST /api/backtest/jobs/task-1/cancel': () =>
        new Promise((r) => {
          resolvePost = r;
        }),
    });
    m = await mount(<JobRow kind="backtest" job={RUNNING} now={NOW} onRefetch={refetch} />);
    await click(buttonByText(m.container, 'Cancel'));
    const primary = buttonByText(m.container, 'Cancel run');
    await act(async () => {
      primary.click();
      primary.click();
    });
    expect(posts()).toHaveLength(1);
    expect(m.container.querySelector('dialog')?.textContent).toContain('Cancelling…');

    await act(async () => resolvePost({ task_id: 'task-1', status: 'cancelling' }));
    await flush();
    expect(posts()).toHaveLength(1);
    expect(refetches).toBe(1);
    expect(m.container.querySelector('dialog[open]')).toBeNull();
  });

  it('says the run already finished on a 409 and refetches', async () => {
    setup({
      'POST /api/backtest/jobs/task-1/cancel': new ApiError(
        409,
        JSON.stringify({ detail: 'Task task-1 is already completed' }),
      ),
    });
    m = await mount(<JobRow kind="backtest" job={RUNNING} now={NOW} onRefetch={refetch} />);
    await click(buttonByText(m.container, 'Cancel'));
    await click(buttonByText(m.container, 'Cancel run'));
    expect(posts()).toHaveLength(1);
    expect(m.container.textContent).toContain('This run already finished.');
    expect(refetches).toBe(1);
    expect(m.container.querySelector('dialog[open]')).toBeNull();
  });
});
