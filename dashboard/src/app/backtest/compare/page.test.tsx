import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flush, mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';

const fetchApi = vi.fn();
const nav = vi.hoisted(() => ({ search: new URLSearchParams() }));

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('next/navigation', () => ({ useSearchParams: () => nav.search }));

const { default: BacktestComparePage } = await import('./page');

function prov(exchange: string, timeframe: string) {
  return {
    exchange,
    symbol: 'BTCUSDT',
    timeframe,
    bar_count: 300,
    first_ts: '2026-01-01T00:00:00+00:00',
    last_ts: '2026-01-13T11:00:00+00:00',
    gap_count: 0,
    data_source: 'db',
  };
}

function result(id: string, provenance: unknown, over: Record<string, unknown> = {}) {
  return {
    id,
    strategy: 'Saw',
    name: id,
    period: 'p',
    status: 'completed',
    provenance,
    metrics: { total_trades: 1, win_rate: 0, total_pnl: 0, max_drawdown: 0, sharpe_ratio: 0 },
    equity_curve: [],
    ...over,
  };
}

const A = result('bt-a', prov('binance', '1h'));
const B = result('bt-b', prov('kraken', '4h'));

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

beforeEach(() => {
  calls = [];
  nav.search = new URLSearchParams();
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

async function choose(container: HTMLElement, index: number, value: string): Promise<void> {
  const select = container.querySelectorAll('select')[index];
  await act(async () => {
    select.value = value;
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await flush();
}

describe('BacktestComparePage', () => {
  it('shows each run data source and warns when the data differs', async () => {
    const routes: Routes = {
      '/api/backtest/results': [A, B],
      '/api/backtest/results/bt-a': A,
      '/api/backtest/results/bt-b': B,
    };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<BacktestComparePage />);

    await choose(page.container, 0, 'bt-a');
    await choose(page.container, 1, 'bt-b');

    expect(page.container.querySelector('[data-testid="data-a"] [data-state="db"]')).not.toBeNull();
    expect(page.container.querySelector('[data-testid="data-b"] [data-state="db"]')).not.toBeNull();
    const warning = page.container.querySelector('[data-testid="provenance-mismatch"]')?.textContent;
    expect(warning).toContain('These runs used different data');
    expect(warning).toContain('exchange: binance vs kraken; timeframe: 1h vs 4h');
  });

  it('prefills both runs from the ?a= and ?b= URL parameters', async () => {
    nav.search = new URLSearchParams('a=bt-a&b=bt-b');
    const routes: Routes = {
      '/api/backtest/results': [A, B],
      '/api/backtest/results/bt-a': A,
      '/api/backtest/results/bt-b': B,
    };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<BacktestComparePage />);

    const selects = page.container.querySelectorAll('select');
    expect(selects[0].value).toBe('bt-a');
    expect(selects[1].value).toBe('bt-b');
    expect(calls.filter((c) => c.path === '/api/backtest/results/bt-a')).toHaveLength(1);
    expect(calls.filter((c) => c.path === '/api/backtest/results/bt-b')).toHaveLength(1);
    expect(page.container.textContent).toContain('Metrics comparison');
  });

  it('renders money as signed USDT with no currency symbol, and names both curves for screen readers', async () => {
    const a = result('bt-a', prov('binance', '1h'), {
      metrics: { total_trades: 1, win_rate: 0, total_pnl: -50, max_drawdown: 0, sharpe_ratio: 0 },
      equity_curve: [
        { timestamp: '2026-01-01T00:00:00+00:00', value: 10000 },
        { timestamp: '2026-01-13T11:00:00+00:00', value: 9950 },
      ],
    });
    const b = result('bt-b', prov('kraken', '4h'), {
      metrics: { total_trades: 2, win_rate: 0, total_pnl: 100, max_drawdown: 0, sharpe_ratio: 0 },
      equity_curve: [
        { timestamp: '2026-01-01T00:00:00+00:00', value: 10000 },
        { timestamp: '2026-01-13T11:00:00+00:00', value: 10100 },
      ],
    });
    const routes: Routes = {
      '/api/backtest/results': [a, b],
      '/api/backtest/results/bt-a': a,
      '/api/backtest/results/bt-b': b,
    };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<BacktestComparePage />);

    await choose(page.container, 0, 'bt-a');
    await choose(page.container, 1, 'bt-b');

    const text = page.container.textContent ?? '';
    expect(text).toContain('Total PnL (USDT)');
    expect(text).toContain('−50.00');
    expect(text).toContain('+100.00');
    expect(text).not.toContain('$');
    expect(page.container.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe(
      'Equity from 2026-01-01 00:00 UTC to 2026-01-13 11:00 UTC: A from 10,000.00 USDT to 9,950.00 USDT, B from 10,000.00 USDT to 10,100.00 USDT',
    );
  });

  it('summarises two 65,000-point equity curves without blowing the call-arg limit', async () => {
    // ts.ohlcv_1m is the stored granularity and the curve is served uncapped:
    // a 45-day 1m-bar run is ~65k points per curve; two of them spread ~130k
    // timestamps into Math.min/Math.max, past V8's call-argument limit.
    const points = (startIso: string) =>
      Array.from({ length: 65_000 }, (_, i) => ({
        timestamp: new Date(Date.parse(startIso) + i * 60_000).toISOString(),
        value: 10_000 + i,
      }));
    const a = result('bt-a', prov('binance', '1m'), {
      equity_curve: points('2026-01-01T00:00:00Z'),
    });
    const b = result('bt-b', prov('kraken', '1m'), {
      equity_curve: points('2026-02-15T00:00:00Z'),
    });
    nav.search = new URLSearchParams('a=bt-a&b=bt-b');
    const routes: Routes = {
      '/api/backtest/results': [a, b],
      '/api/backtest/results/bt-a': a,
      '/api/backtest/results/bt-b': b,
    };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<BacktestComparePage />);

    expect(page.container.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe(
      'Equity from 2026-01-01 00:00 UTC to 2026-04-01 03:19 UTC: A from 10,000.00 USDT to 74,999.00 USDT, B from 10,000.00 USDT to 74,999.00 USDT',
    );
  });

  it('reports an unavailable results list', async () => {
    fetchApi.mockImplementation(routeFetch({ '/api/backtest/results': unavailable() }, calls));
    page = await mount(<BacktestComparePage />);

    expect(page.container.textContent).toContain('Database is not available');
    expect(page.container.textContent).not.toContain('At least two completed backtests');
  });
});
