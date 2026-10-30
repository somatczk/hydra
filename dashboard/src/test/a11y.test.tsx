import axe from 'axe-core';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { dialogPolyfill, mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';
import {
  detail,
  liveStatus,
  paperFill,
  perpHedgedPositions,
  perpPair,
  perpSession,
  perpsState,
  platformConfig,
  session,
} from '@/test/fixtures';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({ id: 'a1b2c3d4-0000-4000-8000-000000000001', sessionId: 'a1b2c3d4-0000-4000-8000-000000000001' }),
}));

const { ThemeProvider } = await import('@/components/layout/ThemeProvider');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');
const pages: Array<[string, () => Promise<{ default: () => ReactNode }>]> = [
  ['/portfolio', () => import('@/app/portfolio/page')],
  ['/settings', () => import('@/app/settings/page')],
  ['/strategies', () => import('@/app/strategies/page')],
  ['/backtest', () => import('@/app/backtest/page')],
  ['/optimize', () => import('@/app/optimize/page')],
  ['/models', () => import('@/app/models/page')],
  ['/builder', () => import('@/app/builder/page')],
  ['/templates', () => import('@/app/templates/page')],
  ['/data', () => import('@/app/data/page')],
  ['/activity', () => import('@/app/activity/page')],
  ['/activity/approvals/[id]', () => import('@/app/activity/approvals/[id]/page')],
  ['/trading/[sessionId]', () => import('@/app/trading/[sessionId]/page')],
];

let page: Mounted | null = null;
afterEach(() => {
  page?.unmount();
  page = null;
});

const populated: Array<[string, () => Promise<{ default: () => ReactNode }>, Routes, string]> = [
  [
    '/portfolio',
    () => import('@/app/portfolio/page'),
    {
      '/api/system/config': { trading_mode: 'paper' },
      '/api/portfolio/summary?source=paper': {
        total_value: 10012.5,
        unrealized_pnl: 1.5,
        realized_pnl: -12.5,
        daily_pnl: 0,
        max_drawdown_pct: 0.2,
        total_fees: 3.25,
        change_pct: 0.1,
        paper_capital_pending: null,
      },
      '/api/portfolio/positions?source=paper': [
        {
          id: 'p1',
          pair: 'BTC/USDT',
          exchange: 'binance',
          side: 'Long',
          size: 0.0015,
          entry_price: 64000,
          current_price: 65000,
          unrealized_pnl: 1.5,
          pnl_pct: 1.5,
        },
      ],
      '/api/portfolio/equity-curve?source=paper': [
        { timestamp: '2026-10-19T00:00:00+00:00', value: 10000 },
        { timestamp: '2026-10-20T00:00:00+00:00', value: 10012.5 },
      ],
      '/api/portfolio/daily-pnl?source=paper': [{ date: '2026-10-20', pnl: 12.5 }],
      '/api/portfolio/monthly-returns?source=paper': [{ month: '2026-10', return_pct: 0.1 }],
      '/api/portfolio/attribution?source=paper': [],
      '/api/strategies': [{ id: 'saw', name: 'Saw' }],
      '/api/portfolio/trades?page=1&source=paper': {
        trades: [
          {
            id: 't1',
            strategy_id: 'saw',
            symbol: 'BTCUSDT',
            side: 'sell',
            timestamp: '2026-10-20T12:00:00+00:00',
            price: 65000,
            quantity: 0.0015,
            pnl: -12.5,
            notes: 'note',
            tags: ['a'],
          },
        ],
        total: 1,
        page: 1,
        limit: 20,
        pages: 1,
      },
    },
    'loss 12.50 USDT',
  ],
  [
    '/settings',
    () => import('@/app/settings/page'),
    {
      '/api/system/exchanges': [
        {
          id: 'binance',
          name: 'Binance',
          supported: true,
          connected: true,
          api_key_set: true,
          credential_state: 'valid',
          last_sync: '2026-10-21T08:00:00+00:00',
          environment: 'testnet',
          validated_environment: 'testnet',
        },
      ],
      '/api/system/config': {
        trading_mode: 'paper',
        default_pair: 'BTC/USDT',
        default_timeframe: '1h',
        max_concurrent_strategies: 5,
        paper_capital: 10000,
        paper_capital_pending: null,
        webhook_secret_set: true,
      },
    },
    'Binance',
  ],
  [
    '/backtest',
    () => import('@/app/backtest/page'),
    {
      '/api/backtest/results': [
        {
          id: 'bt-1',
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
        },
      ],
      '/api/backtest/strategies': [{ id: 'saw', name: 'Saw', primary_timeframe: '4h' }],
      '/api/backtest/jobs?kind=backtest&limit=50': [
        {
          task_id: 'task-1',
          kind: 'backtest',
          status: 'completed',
          progress: 100,
          created_at: '2026-10-21T08:00:00+00:00',
          finished_at: '2026-10-21T08:05:00+00:00',
          strategy_id: 'saw',
          name: 'real',
          error: null,
          result_id: 'bt-1',
          completed_trials: null,
          total_trials: null,
        },
      ],
    },
    'Completed',
  ],
  [
    '/trading/[sessionId]',
    () => import('@/app/trading/[sessionId]/page'),
    {
      // P7-G's hedge-mode surfaces: both-side positions, the perps block and the resting pair.
      '/api/trading/sessions/a1b2c3d4-0000-4000-8000-000000000001/detail': detail({
        ...perpSession(),
        symbols: ['BTCUSDT', 'ETHUSDT'],
        positions: perpHedgedPositions(),
        perps: perpsState(),
        open_orders: perpPair(),
        trades: [paperFill()],
      }),
      '/api/risk/live-status': liveStatus(),
      '/api/trading/sessions': [session()],
      '/api/system/config': platformConfig(),
      '/api/market/orderbook?symbol=BTCUSDT': {
        symbol: 'BTCUSDT',
        bids: [[65000.5, 1.25]],
        asks: [[65001, 0.75]],
        spread: 0.5,
        timestamp: null,
        fetched_at: new Date().toISOString(),
      },
      '/api/strategies/sma-cross-btc-1h': { risk: { stop_loss_method: 'fixed_pct', stop_loss_value: 5 } },
    },
    'One-way position mode',
  ],
];

/** The populated pages' paper fixtures in one route set, plus the shell's own paper sources. */
function paperRoutes(): Routes {
  return {
    '/api/risk/live-status': liveStatus(),
    '/api/trading/sessions': [],
    ...Object.assign({}, ...populated.map(([, , routes]) => routes)),
  };
}

async function mountPage(load: () => Promise<{ default: () => ReactNode }>) {
  const { default: Page } = await load();
  page = await mount(
    <ThemeProvider>
      <ShellStatusProvider>
        <main>
          <Page />
        </main>
      </ShellStatusProvider>
    </ThemeProvider>,
  );
}

async function axeViolations(load: () => Promise<{ default: () => ReactNode }>) {
  await mountPage(load);
  // The session page's TradingView chart is a cross-origin iframe jsdom cannot host, and
  // axe cannot walk into it (its internals are TradingView's, not this app's surface).
  page!.container.querySelectorAll('iframe').forEach((f) => f.remove());
  const results = await axe.run(page!.container, {
    rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
  });
  return results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(' | ')}`);
}

describe('axe (jsdom: structure, names and roles; contrast and layout are checked by hand)', () => {
  it.each(pages)('%s has no violations when its data is unavailable', async (_route, load) => {
    dialogPolyfill();
    fetchApi.mockReset();
    fetchApi.mockRejectedValue(unavailable());
    expect(await axeViolations(load)).toEqual([]);
  });

  it.each(populated)('%s has no violations with data loaded', async (_route, load, routes, marker) => {
    dialogPolyfill();
    fetchApi.mockReset();
    fetchApi.mockImplementation(routeFetch(routes, []));
    expect(await axeViolations(load)).toEqual([]);
    expect(page?.container.textContent).toContain(marker);
  });
});

/** The all-paper world (08 §6): every page's paper data, no live-mode claim in the rendered copy.
 * A non-open <dialog> is display:none, so its content is not rendered copy; the 'armed' scan is over
 * rendered text only — the word exists in code (protection === 'armed'). */
describe('the all-paper world (08 §6)', () => {
  it.each(pages)('%s renders no LIVE, real-money or armed copy', async (_route, load) => {
    dialogPolyfill();
    fetchApi.mockReset();
    fetchApi.mockImplementation(routeFetch(paperRoutes(), []));
    await mountPage(load);
    const rendered = page!.container.cloneNode(true) as HTMLElement;
    rendered.querySelectorAll('dialog:not([open])').forEach((d) => d.remove());
    const text = rendered.textContent ?? '';
    expect(text).not.toMatch(/\bLIVE\b/);
    expect(text).not.toContain('real money');
    expect(text).not.toContain('Live trading is on');
    expect(text).not.toMatch(/\barmed\b/);
    page?.unmount();
    page = null;
  });
});
