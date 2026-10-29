import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flush, mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { default: DataPage } = await import('./page');

/** The page's own compact format, so assertions never pin an ICU string. */
const VOLUME = new Intl.NumberFormat('en-GB', { notation: 'compact', maximumFractionDigits: 1 });

/** The stored ranking exactly as GET /api/market/universe serves it. */
const UNIVERSE = {
  exchange_id: 'binance',
  quote_asset: 'USDT',
  source: 'binance spot rolling-24h quote volume (GET /api/v3/ticker/24hr)',
  ranked_at: '2026-10-29T08:00:00+00:00',
  age_hours: 2,
  count: 3,
  symbols: [
    { rank: 1, symbol: 'BTCUSDT', quote_volume: 2_000_000_000 },
    { rank: 2, symbol: 'ETHUSDT', quote_volume: 800_000_000 },
    { rank: 3, symbol: 'SOLUSDT', quote_volume: 300_000_000 },
  ],
};

/** The data_inventory tool's reads exactly as GET /api/market/data-inventory serves them. */
function inventory(over: Record<string, unknown> = {}) {
  return {
    datasets: [
      {
        exchange: 'binance',
        symbol: 'ETHUSDT',
        timeframe: '1m',
        coverage: { first_ts: '2025-12-01T00:00:00+00:00', last_ts: '2026-01-31T23:00:00+00:00' },
        rows: 500,
        estimated_bytes: 80_000,
        status: 'ready',
        last_used_at: '2026-10-02T00:00:00+00:00',
      },
      {
        exchange: 'binance',
        symbol: 'BTCUSDT',
        timeframe: '1h',
        coverage: { first_ts: '2025-12-01T00:00:00+00:00', last_ts: '2026-01-31T23:00:00+00:00' },
        rows: 0,
        estimated_bytes: 0,
        status: 'fetching',
        last_used_at: '2026-10-03T00:00:00+00:00',
      },
    ],
    totals: {
      bytes: 80_000,
      rows: 500,
      pairs: 2,
      budget_bytes: 10 * (1 << 30),
      // 30 min before the real clock: inside the two-interval sweep-stall threshold
      last_sweep: new Date(Date.now() - 30 * 60_000).toISOString(),
    },
    sweep_interval_s: 3600,
    ...over,
  };
}

let m: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

function routes(universe: unknown = UNIVERSE, cache: unknown = inventory()): Routes {
  return { '/api/market/universe': universe, '/api/market/data-inventory': cache };
}

async function mountPage(r: Routes = routes()): Promise<void> {
  fetchApi.mockImplementation(routeFetch(r, calls));
  m = await mount(<DataPage />);
  await flush();
}

beforeEach(() => {
  fetchApi.mockReset();
  calls = [];
});

afterEach(() => {
  m?.unmount();
  m = null;
});

describe('DataPage universe', () => {
  it('renders the members in rank order with the ranking provenance and freshness', async () => {
    await mountPage();
    const text = m!.container.textContent ?? '';

    expect(text).toContain('Top-100 universe');
    expect(text).toContain('3 binance USDT pairs by rolling-24h quote volume');
    expect(text).toContain('ranked 2 h ago');
    expect(text).toContain('Source: binance spot rolling-24h quote volume (GET /api/v3/ticker/24hr)');
    expect(text).toContain('it only blocks new saves until it re-enters');
    const cells = Array.from(m!.container.querySelectorAll('table td')).map((td) => td.textContent);
    expect(cells).toContain('#1');
    expect(cells).toContain('BTCUSDT');
    expect(cells).toContain(VOLUME.format(2_000_000_000));
    expect(cells).toContain('#3');
    expect(cells).toContain('SOLUSDT');
    expect(m!.container.querySelector('[data-state="stale"]')).toBeNull();
    expect(text).not.toContain('SWEEP STALLED');
  });

  it('flags a ranking older than two refresh days instead of presenting it as fresh', async () => {
    await mountPage(routes({ ...UNIVERSE, age_hours: 96, ranked_at: '2026-10-25T08:00:00+00:00' }));
    const text = m!.container.textContent ?? '';

    expect(text).toContain('ranked at 25 Oct');
    expect(m!.container.querySelector('[data-state="stale"]')).not.toBeNull();
  });

  it('shows the server reason while the universe is unreadable, never an empty table', async () => {
    await mountPage(routes(unavailable('The top-100 universe has not been computed yet')));
    const text = m!.container.textContent ?? '';

    expect(m!.container.querySelector('[data-state="unavailable"]')).not.toBeNull();
    expect(text).toContain('The top-100 universe has not been computed yet');
    expect(text).not.toContain('#1');
    expect(text).not.toContain(`BTCUSDT${VOLUME.format(2_000_000_000)}`);
  });

  it('renders the loading skeletons before the first answer, never an empty table', async () => {
    fetchApi.mockImplementation(
      routeFetch(
        {
          '/api/market/universe': new Promise(() => {}),
          '/api/market/data-inventory': new Promise(() => {}),
        },
        calls,
      ),
    );
    m = await mount(<DataPage />);

    expect(m.container.querySelectorAll('table').length).toBeGreaterThan(0);
    expect(m.container.textContent).not.toContain('No members');
    expect(m.container.textContent).not.toContain('No auto-cached datasets');
  });
});

describe('DataPage cache inventory', () => {
  it('renders the totals against the budget, the last sweep, and every dataset LRU-first', async () => {
    await mountPage();
    const text = m!.container.textContent ?? '';

    expect(text).toContain('78.1 KB of 10.0 GB (0%)');
    expect(text).toContain('500 bars · 2 pairs');
    expect(text).toContain('Last sweep:');
    expect(text).toContain('the sweeper runs every 60 min');
    const rows = Array.from(m!.container.querySelectorAll('table tbody tr')).map((tr) => tr.textContent);
    // the inventory table is the page's last one (the universe table renders above it)
    const inventoryRows = rows.slice(-2);
    expect(inventoryRows[0]).toContain('ETHUSDT');
    expect(inventoryRows[0]).toContain('READY');
    expect(inventoryRows[0]).toContain('1 Dec 2025 00:00');
    expect(inventoryRows[1]).toContain('BTCUSDT');
    expect(inventoryRows[1]).toContain('0 (no bars)');
    expect(inventoryRows[1]).toContain('FETCHING');
    expect(text).toContain('it can predate the first stored bar');
  });

  it('states over-budget and a stalled sweep honestly instead of a quiet percent', async () => {
    await mountPage(
      routes(
        UNIVERSE,
        inventory({
          totals: {
            bytes: 12 * (1 << 30),
            rows: 1_000_000,
            pairs: 4,
            budget_bytes: 10 * (1 << 30),
            // five sweep intervals before the real clock: over the two-interval stall threshold
            last_sweep: new Date(Date.now() - 5 * 3600_000).toISOString(),
          },
        }),
      ),
    );
    const text = m!.container.textContent ?? '';

    expect(text).toContain('OVER BUDGET');
    expect(text).toContain('120%');
    expect(text).toContain('SWEEP STALLED');
  });

  it('names the disabled budget as delete-after-run, with no percent', async () => {
    await mountPage(
      routes(
        UNIVERSE,
        inventory({
          totals: { bytes: 80_000, rows: 500, pairs: 2, budget_bytes: 0, last_sweep: null },
        }),
      ),
    );
    const text = m!.container.textContent ?? '';

    expect(text).toContain('delete-after-run (cache_max_gb=0)');
    expect(text).toContain('none yet');
    expect(text).not.toContain('%');
  });

  it('shows the honest empty cache, not a fabricated one', async () => {
    await mountPage(
      routes(
        UNIVERSE,
        inventory({ datasets: [], totals: { bytes: 0, rows: 0, pairs: 0, budget_bytes: 10 * (1 << 30), last_sweep: null } }),
      ),
    );
    const text = m!.container.textContent ?? '';

    expect(text).toContain('No auto-cached datasets yet');
    expect(text).toContain('0 B of 10.0 GB (0%)');
    expect(text).toContain('none yet');
  });

  it('shows the server reason while the inventory is unreadable', async () => {
    await mountPage(routes(UNIVERSE, unavailable('Database is not available')));
    const text = m!.container.textContent ?? '';

    expect(m!.container.querySelectorAll('[data-state="unavailable"]').length).toBeGreaterThan(0);
    expect(text).toContain('Database is not available');
    expect(text).not.toContain('No auto-cached datasets yet');
  });
});
