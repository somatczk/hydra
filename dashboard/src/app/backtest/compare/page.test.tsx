import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flush, mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

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

function result(id: string, provenance: unknown) {
  return {
    id,
    strategy: 'Saw',
    name: id,
    period: 'p',
    status: 'completed',
    provenance,
    metrics: { total_trades: 1, win_rate: 0, total_pnl: 0, max_drawdown: 0, sharpe_ratio: 0 },
    equity_curve: [],
  };
}

const A = result('bt-a', prov('binance', '1h'));
const B = result('bt-b', prov('kraken', '4h'));

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
    expect(page.container.querySelector('[data-testid="provenance-mismatch"]')?.textContent).toContain(
      'exchange: binance vs kraken; timeframe: 1h vs 4h',
    );
  });

  it('reports an unavailable results list', async () => {
    fetchApi.mockImplementation(routeFetch({ '/api/backtest/results': unavailable() }, calls));
    page = await mount(<BacktestComparePage />);

    expect(page.container.textContent).toContain('Database is not available');
    expect(page.container.textContent).not.toContain('At least two completed backtests');
  });
});
