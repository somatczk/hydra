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

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: nav.push }),
  useParams: () => ({ id: 'bt-task-1' }),
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

  it('gives the equity chart a value-bearing summary for screen readers', async () => {
    fetchApi.mockImplementation(routeFetch(routes({ ...DETAIL, equity_curve: EQUITY, benchmark_equity: [] }), calls));
    page = await mount(<BacktestDetailPage />);

    expect(page.container.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe(
      'Equity from 2026-01-01 00:00 UTC to 2026-01-13 11:00 UTC: start 10,000.00 USDT, end 10,500.00 USDT',
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

  it('surfaces no Share control and deletes only through a title-focused confirm', async () => {
    const mutations: Mutations = { 'DELETE /api/backtest/results/bt-task-1': {} };
    fetchApi.mockImplementation(routeFetch(routes(DETAIL), calls, mutations));
    page = await mount(<BacktestDetailPage />);

    expect(page.container.textContent).not.toContain('Share');

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

    expect(page.container.querySelector('a[href="/backtest/compare?a=bt-task-1"]')).not.toBeNull();
    expect(page.container.textContent).toContain('Compare with…');
  });
});
