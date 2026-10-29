import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { liveStatus, platformConfig, session } from '@/test/fixtures';
import {
  buttonByText,
  click,
  dialogPolyfill,
  mount,
  routeFetch,
  unavailable,
  type Mounted,
  type Mutations,
  type Routes,
} from '@/test/pageHarness';

const fetchApi = vi.fn();
const nav = vi.hoisted(() => ({ push: vi.fn() }));
const route = vi.hoisted(() => ({ params: { id: 'bt-task-1' } as Record<string, string> }));

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: nav.push }),
  useParams: () => route.params,
}));

const { default: BacktestDetailPage } = await import('./page');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');

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

const EQUITY = [
  { timestamp: '2026-01-01T00:00:00+00:00', value: 10000 },
  { timestamp: '2026-01-13T11:00:00+00:00', value: 10500 },
];

const VERIFY_MATCH = {
  trade_count_match: true,
  win_rate_match: true,
  total_pnl_match: true,
  computed_trade_count: 12,
  computed_win_rate: 50,
  computed_total_pnl: -50,
  reported_trade_count: 12,
  reported_win_rate: 50,
  reported_total_pnl: -50,
  all_passed: true,
};

const VERIFY_MISMATCH = {
  ...VERIFY_MATCH,
  trade_count_match: false,
  win_rate_match: false,
  all_passed: false,
};

const MISMATCH_SENTENCE = 'Metrics recomputed from 12 stored trades: mismatch on trade count, win rate';

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

function routes(detail: unknown, verify: unknown = new ApiError(404, '{}')): Routes {
  return {
    '/api/backtest/results/bt-task-1': detail,
    '/api/backtest/results/bt-task-1/verify': verify,
  };
}

beforeEach(() => {
  dialogPolyfill();
  calls = [];
  fetchApi.mockReset();
  nav.push.mockReset();
  route.params = { id: 'bt-task-1' };
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
    expect(text).toContain('−316.67');
    expect(text).toContain('Expectancy (USDT)');
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
    expect(text).not.toContain('Max consec. losses');
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

  it('shows money as signed USDT and never a currency symbol', async () => {
    const detail = {
      ...DETAIL,
      strategy_id: 'saw-1',
      metrics: {
        ...DETAIL.metrics,
        total_pnl: -50,
        win_rate: 42.9,
        sharpe_ratio: 1.23,
        max_drawdown: 8.3,
        total_trades: 7,
      },
      equity_curve: EQUITY,
      benchmark_equity: EQUITY,
      transactions: [
        { trade_id: 1, type: 'entry', time: '2026-01-02T00:00:00+00:00', side: 'Buy', price: 67612.4, quantity: 0.0015, fee: 0.1014, pnl: null },
        { trade_id: 1, type: 'exit', time: '2026-01-03T00:00:00+00:00', side: 'Sell', price: 68000, quantity: 0.0015, fee: 0.102, pnl: 0.57 },
      ],
    };
    fetchApi.mockImplementation(routeFetch(routes(detail, VERIFY_MATCH), calls));
    page = await mount(<BacktestDetailPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Total PnL (USDT)');
    expect(text).toContain('−50.00');
    expect(text).toContain('Price (USDT)');
    expect(text).toContain('67,612.40');
    expect(text).toContain('0.1014');
    expect(text).toContain('+0.57');
    expect(text).not.toContain('$');
  });

  it('tells screen readers gain or loss on signed money, which stays uncoloured', async () => {
    const detail = {
      ...DETAIL,
      metrics: { ...DETAIL.metrics, total_pnl: -50, max_drawdown: 8.3, expectancy: -316.67 },
      transactions: [
        { trade_id: 1, type: 'entry', time: '2026-01-02T00:00:00+00:00', side: 'Buy', price: 67612.4, quantity: 0.0015, fee: 0.1014, pnl: null },
        { trade_id: 1, type: 'exit', time: '2026-01-03T00:00:00+00:00', side: 'Sell', price: 68000, quantity: 0.0015, fee: 0.102, pnl: 0.57 },
        { trade_id: 2, type: 'entry', time: '2026-01-04T00:00:00+00:00', side: 'Buy', price: 68000, quantity: 0.0015, fee: 0.102, pnl: null },
        { trade_id: 2, type: 'exit', time: '2026-01-05T00:00:00+00:00', side: 'Sell', price: 67000, quantity: 0.0015, fee: 0.1005, pnl: -12.5 },
      ],
    };
    fetchApi.mockImplementation(routeFetch(routes(detail), calls));
    page = await mount(<BacktestDetailPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('loss 50.00 USDT');
    expect(text).toContain('loss 316.67 USDT');
    expect(text).toContain('gain 0.57 USDT');
    expect(text).toContain('loss 12.50 USDT');
    // the sign glyph is replaced, not doubled, for assistive tech
    const hidden = Array.from(page.container.querySelectorAll('span[aria-hidden="true"]')).map((s) => s.textContent);
    expect(hidden).toContain('−50.00');
    expect(hidden).toContain('+0.57');
    // no signed-money element sits in the danger colour (drawdown/Sell badges may)
    for (const visible of ['−50.00', '−316.67', '+0.57', '−12.50']) {
      const el = Array.from(page.container.querySelectorAll('span[aria-hidden="true"]')).find(
        (s) => s.textContent === visible,
      );
      expect(el, `visible ${visible}`).toBeDefined();
      expect(el?.closest('.text-danger'), `visible ${visible}`).toBeNull();
    }
  });

  it('gives the equity chart a value-bearing summary for screen readers', async () => {
    fetchApi.mockImplementation(routeFetch(routes({ ...DETAIL, equity_curve: EQUITY, benchmark_equity: [] }), calls));
    page = await mount(<BacktestDetailPage />);

    expect(page.container.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe(
      'Equity from 2026-01-01 00:00 UTC to 2026-01-13 11:00 UTC: start 10,000.00 USDT, end 10,500.00 USDT',
    );
  });

  it('adds the benchmark start and end to the chart summary when one was recorded', async () => {
    const bench = [
      { timestamp: '2026-01-01T00:00:00+00:00', value: 9800 },
      { timestamp: '2026-01-13T11:00:00+00:00', value: 9900 },
    ];
    fetchApi.mockImplementation(routeFetch(routes({ ...DETAIL, equity_curve: EQUITY, benchmark_equity: bench }), calls));
    page = await mount(<BacktestDetailPage />);

    expect(page.container.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe(
      'Equity from 2026-01-01 00:00 UTC to 2026-01-13 11:00 UTC: start 10,000.00 USDT, end 10,500.00 USDT, benchmark from 9,800.00 USDT to 9,900.00 USDT',
    );
  });

  it('renders the verify mismatch sentence in a caution tone, never the old danger form', async () => {
    fetchApi.mockImplementation(routeFetch(routes(DETAIL, VERIFY_MISMATCH), calls));
    page = await mount(<BacktestDetailPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain(MISMATCH_SENTENCE);
    expect(text).not.toContain('mismatch:');
    const badge = Array.from(page.container.querySelectorAll('span')).find(
      (s) => s.textContent === MISMATCH_SENTENCE,
    );
    expect(badge?.className).toContain('bg-caution-soft');
    expect(badge?.className).not.toContain('bg-danger-soft');
  });

  it('keeps the verify match sentence when verification passes', async () => {
    fetchApi.mockImplementation(routeFetch(routes(DETAIL, VERIFY_MATCH), calls));
    page = await mount(<BacktestDetailPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Metrics recomputed from 12 stored trades: match');
    expect(text).not.toContain('mismatch');
  });

  it('mints no share link until asked and deletes only through a title-focused confirm', async () => {
    const mutations: Mutations = { 'DELETE /api/backtest/results/bt-task-1': {} };
    fetchApi.mockImplementation(routeFetch(routes(DETAIL), calls, mutations));
    page = await mount(<BacktestDetailPage />);

    expect(calls.some((c) => c.path.endsWith('/share'))).toBe(false);

    await click(buttonByText(page.container, 'Delete'));
    const dialog = page.container.querySelector('dialog[open]');
    expect(dialog?.textContent).toContain('Delete this backtest?');
    expect(document.activeElement?.textContent).toBe('Delete this backtest?');
    expect(calls.filter((c) => c.init?.method === 'DELETE')).toHaveLength(0);

    await click(buttonByText(page.container, 'Delete backtest'));
    expect(calls.filter((c) => c.init?.method === 'DELETE')).toHaveLength(1);
    expect(nav.push).toHaveBeenCalledWith('/backtest');
  });

  it('starts a paper session with this strategy from the header door', async () => {
    const mutations: Mutations = {
      'POST /api/trading/sessions': session({ session_id: 'new-1', strategy_id: 'saw-1' }),
    };
    fetchApi.mockImplementation(
      routeFetch(
        {
          ...routes({ ...DETAIL, strategy_id: 'saw-1' }),
          '/api/risk/live-status': liveStatus(),
          '/api/trading/sessions': [],
          '/api/system/config': platformConfig(),
          '/api/strategies/saw-1': { id: 'saw-1', name: 'Saw', symbol: 'BTCUSDT', timeframes: { primary: '1h' } },
        },
        calls,
        mutations,
      ),
    );
    page = await mount(
      <ShellStatusProvider>
        <BacktestDetailPage />
      </ShellStatusProvider>,
    );

    await click(buttonByText(page.container, 'Start paper session'));
    const dialog = page.container.querySelector('dialog[open]');
    expect(dialog?.textContent).toContain('Start a paper session');
    expect(dialog?.textContent).toContain('Saw · Binance spot · BTCUSDT · 1h · PAPER');
    expect(calls.filter((c) => c.init?.method === 'POST')).toHaveLength(0);

    const primary = Array.from(page.container.querySelectorAll('button'))
      .filter((b) => b.textContent?.trim() === 'Start paper session')
      .pop();
    expect(primary).toBeDefined();
    await click(primary as HTMLButtonElement);
    const post = calls.find((c) => c.init?.method === 'POST');
    expect(JSON.parse(post?.init?.body as string)).toEqual({ strategy_id: 'saw-1', trading_mode: 'paper' });
    expect(nav.push).toHaveBeenCalledWith('/trading/new-1');
  });

  it('hides the start door when the result carries no strategy_id', async () => {
    fetchApi.mockImplementation(routeFetch(routes(DETAIL), calls));
    page = await mount(<BacktestDetailPage />);

    expect(page.container.textContent).not.toContain('Start paper session');
  });

  it('links to Compare with this result prefilled', async () => {
    fetchApi.mockImplementation(routeFetch(routes(DETAIL), calls));
    page = await mount(<BacktestDetailPage />);

    const link = page.container.querySelector('a[href="/backtest/compare?a=bt-task-1"]');
    expect(link).not.toBeNull();
    expect(page.container.textContent).toContain('Compare with…');
    expect(link?.classList.contains('focus-visible:outline-2')).toBe(true);
    expect(link?.classList.contains('focus-visible:outline-offset-2')).toBe(true);
    expect(link?.classList.contains('focus-visible:outline-accent')).toBe(true);
  });
});

describe('BacktestDetailPage auto-fetch record', () => {
  it('shows what the worker fetched before the run, from the bt- result\'s job row', async () => {
    const fetch = {
      state: 'fetched',
      ranges: [['2026-01-01T00:00:00+00:00', '2026-01-13T11:00:00+00:00']],
      bars_fetched: 300,
      weight_spent: 6,
      elapsed_ms: 9000,
    };
    fetchApi.mockImplementation(routeFetch({ ...routes(DETAIL), '/api/backtest/status/task-1': { fetch, fetch_error: null } }, calls));
    page = await mount(<BacktestDetailPage />);

    expect(page.container.querySelector('[data-testid="result-fetch"]')?.textContent).toBe(
      'Fetched 300 missing bars over 1 range (1 Jan 2026 – 13 Jan 2026) before the run · weight 6 · 9 s',
    );
  });

  it('shows nothing for a result without a job row, and says so when the row cannot be read', async () => {
    fetchApi.mockImplementation(routeFetch(routes(DETAIL), calls));
    page = await mount(<BacktestDetailPage />);
    expect(calls.some((c) => c.path === '/api/backtest/status/task-1')).toBe(true);
    expect(page.container.querySelector('[data-testid="result-fetch"]')).toBeNull();
    expect(page.container.querySelector('[data-testid="result-fetch-error"]')).toBeNull();
    page.unmount();

    fetchApi.mockImplementation(routeFetch({ ...routes(DETAIL), '/api/backtest/status/task-1': unavailable() }, calls));
    page = await mount(<BacktestDetailPage />);
    expect(page.container.querySelector('[data-testid="result-fetch-error"]')?.textContent).toContain(
      'Auto-fetch record: Database is not available',
    );
  });
});

describe('BacktestDetailPage share links', () => {
  it('creates a link to the dashboard view, copies it and says it is not public and ends on restart', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    fetchApi.mockImplementation(
      routeFetch(routes(DETAIL), calls, {
        'POST /api/backtest/results/bt-task-1/share': { token: 'a1b2c3d4e5f6', share_url: '/api/backtest/shared/a1b2c3d4e5f6' },
      }),
    );
    page = await mount(<BacktestDetailPage />);

    await click(buttonByText(page.container, 'Share link'));
    const box = page.container.querySelector('[data-testid="share-link"]')!;
    const url = `${window.location.origin}/backtest/shared/a1b2c3d4e5f6`;
    expect((box.querySelector('input') as HTMLInputElement).value).toBe(url);
    expect(box.textContent).toContain('it is not public');
    expect(box.textContent).toContain('a restart or redeploy ends every link, and there is no revoke');

    await click(buttonByText(box as HTMLElement, 'Copy'));
    expect(writeText).toHaveBeenCalledWith(url);
  });

  it('renders a shared link read-only from the shared endpoint, with no actions and no verify read', async () => {
    route.params = { token: 'a1b2c3d4e5f6' };
    fetchApi.mockImplementation(routeFetch({ '/api/backtest/shared/a1b2c3d4e5f6': DETAIL }, calls));
    page = await mount(<BacktestDetailPage />);

    expect(page.container.querySelector('[data-testid="shared-banner"]')?.textContent).toContain('read-only');
    expect(page.container.querySelector('[data-testid="provenance-panel"]')?.textContent).toContain('binance BTCUSDT 1h');
    const buttons = Array.from(page.container.querySelectorAll('button')).map((b) => b.textContent?.trim());
    for (const action of ['Share link', 'Delete', 'Start paper session']) expect(buttons).not.toContain(action);
    expect(page.container.querySelector('[aria-label="Rename this backtest"]')).toBeNull();
    expect(page.container.textContent).not.toContain('Compare with…');
    expect(calls.some((c) => c.path.includes('/verify'))).toBe(false);
  });

  it('says a dead link ended with a restart', async () => {
    route.params = { token: 'gone' };
    fetchApi.mockImplementation(
      routeFetch({ '/api/backtest/shared/gone': new ApiError(404, JSON.stringify({ detail: 'Shared link not found or expired' })) }, calls),
    );
    page = await mount(<BacktestDetailPage />);

    expect(page.container.textContent).toContain('This share link no longer works: links end when the engine restarts.');
  });
});
