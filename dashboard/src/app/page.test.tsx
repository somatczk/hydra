import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';
import { breaker, detail, liveStatus, platformConfig, session } from '@/test/fixtures';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { default: Page, metadata } = await import('./page');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');

const SUMMARY = {
  total_value: 10100,
  unrealized_pnl: -38.62,
  realized_pnl: 124.1,
  daily_pnl: 51.5,
  max_drawdown_pct: 0,
  total_fees: 3.2,
  change_pct: 0,
  paper_capital_pending: null,
};
const RISK = { current_drawdown: 1.84, max_drawdown_limit: 15, circuit_breakers: [breaker()] };

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

function routes(over: Routes = {}): Routes {
  return {
    '/api/risk/live-status': liveStatus(),
    '/api/trading/sessions': [session({ session_id: 'run-a', strategy_name: 'SMA cross' })],
    '/api/system/config': platformConfig(),
    '/api/risk/status': RISK,
    '/api/portfolio/summary?source=paper': SUMMARY,
    '/api/portfolio/equity-curve?source=paper': [
      { timestamp: '2026-10-20T00:00:00+00:00', value: 10000 },
      { timestamp: '2026-10-21T00:00:00+00:00', value: 10100 },
    ],
    '/api/system/readiness': { stale: false, checks: { feeds: { ok: true, streams: [{ ok: true }] } } },
    '/api/risk/events?limit=500': [],
    '/api/trading/sessions/run-a/detail': () => detail({ session_id: 'run-a', last_bar_at: new Date().toISOString() }),
    ...over,
  };
}

async function show(over: Routes = {}) {
  page?.unmount();
  fetchApi.mockImplementation(routeFetch(routes(over), calls));
  page = await mount(
    <ShellStatusProvider>
      <Page />
    </ShellStatusProvider>,
  );
  return page.container;
}

const tile = (c: HTMLElement, label: string) =>
  Array.from(c.querySelectorAll('a')).find((a) => a.textContent?.startsWith(label))?.textContent ?? '';

beforeEach(() => {
  calls = [];
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('Status page', () => {
  it('owns the title and a single h1', async () => {
    const c = await show();
    expect(metadata.title).toBe('Status');
    expect(c.querySelectorAll('h1')).toHaveLength(1);
    expect(c.querySelector('h1')?.textContent).toBe('Status');
  });

  it('answers the four questions as links', async () => {
    const c = await show();
    expect(tile(c, 'Running')).toContain('1 session running');
    expect(tile(c, 'Safe')).toContain('Kill switch off · Tier 0 · Drawdown 1.84% of 15.00% · New entries allowed');
    expect(tile(c, 'Data')).toContain('On time · 1 of 1 sessions');
    expect(tile(c, 'Data')).toContain('Feed check passed');
    expect(tile(c, 'Needs you')).toContain('Nothing needs you');
  });

  it('shows the paper account from the paper source and never calls live-metrics or news', async () => {
    const c = await show();
    expect(c.textContent).toContain('10,100.00');
    expect(c.textContent).toContain('Realised today');
    expect(c.textContent).toContain('+51.50 USDT');
    expect(c.textContent).toContain('−38.62 USDT');
    expect(calls.some((x) => x.path.includes('live-metrics') || x.path.includes('news'))).toBe(false);
    expect(c.textContent).not.toContain('Sharpe');
  });

  it('marks a late session in the Data answer', async () => {
    const late = new Date(Date.now() - 3 * 3600_000).toISOString();
    const c = await show({ '/api/trading/sessions/run-a/detail': detail({ session_id: 'run-a', last_bar_at: late }) });
    expect(tile(c, 'Data')).toContain('1 of 1 late: SMA cross 1h bar 2 h 0 min overdue');
  });

  it('says UNKNOWN, not on time, when the session details cannot be read', async () => {
    const c = await show({ '/api/trading/sessions/run-a/detail': unavailable() });
    expect(tile(c, 'Data')).toContain("Couldn't read session health");
    expect(tile(c, 'Data')).toContain('UNKNOWN');
  });

  it('lists what needs you with a link, and "?" when a source is unreadable', async () => {
    const c = await show({
      '/api/trading/sessions': [session({ session_id: 'run-a', status: 'error', error_message: 'feed lost' })],
      '/api/system/config': unavailable(),
    });
    expect(tile(c, 'Needs you')).toContain('?');
    expect(tile(c, 'Needs you')).not.toContain('Nothing needs you');
    expect(c.textContent).toContain("Couldn't check everything: settings unavailable");
    expect(c.querySelector('li a[href="/trading/run-a"]')?.textContent).toContain('SMA cross stopped with an error: feed lost');
  });

  it('says the risk state is unavailable and never claims entries are allowed', async () => {
    const c = await show({ '/api/risk/status': unavailable() });
    expect(tile(c, 'Safe')).toContain("Risk state couldn't be loaded. Opening orders are refused until it recovers.");
    expect(tile(c, 'Safe')).not.toContain('New entries allowed');
  });

  it('reports blocked entries from the breaker restrictions', async () => {
    const c = await show({
      '/api/risk/live-status': liveStatus({
        circuit_breaker_restrictions: {
          active_tier: 2,
          can_open_new_trades: false,
          size_multiplier: 0,
          must_flatten: false,
          is_shutdown: false,
          daily_halt_until: null,
          consecutive_pause_until: null,
        },
      }),
    });
    expect(tile(c, 'Safe')).toContain('New entries blocked: breaker tier 2');
  });

  it('counts risk events from the last 24 hours', async () => {
    const now = Date.now();
    const events = [
      { ts: new Date(now - 3600_000).toISOString() },
      { ts: new Date(now - 2 * 3600_000).toISOString() },
      { ts: new Date(now - 48 * 3600_000).toISOString() },
    ];
    const c = await show({ '/api/risk/events?limit=500': events });
    expect(c.textContent).toContain('Since yesterday: 2 risk events');
  });

  it('shows the account failure with the server message', async () => {
    const c = await show({ '/api/portfolio/summary?source=paper': unavailable('ledger not ready') });
    expect(c.textContent).toContain('UNAVAILABLE');
    expect(c.textContent).toContain('ledger not ready');
  });

  it('refuses to show an account value of zero as data', async () => {
    const c = await show({ '/api/portfolio/summary?source=paper': { ...SUMMARY, total_value: 0 } });
    expect(c.textContent).toContain("Account value couldn't be determined");
  });

  it('describes the equity chart for screen readers and reports no history', async () => {
    const c = await show();
    expect(c.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe(
      'Equity from 10,000.00 to 10,100.00 USDT, peak 10,100.00 USDT.',
    );
    const c2 = await show({ '/api/portfolio/equity-curve?source=paper': [] });
    expect(c2.textContent).toContain('No equity history yet.');
  });

  it('shows the running-sessions table row from its detail', async () => {
    const c = await show();
    const row = c.querySelector('tbody tr')?.textContent ?? '';
    expect(row).toContain('SMA cross');
    expect(row).toContain('on time');
    expect(row).toContain('0 positions');
    expect(c.querySelectorAll('th[scope="col"]').length).toBeGreaterThan(0);
  });

  it('shows no feed check under a Data answer that says nothing is checked', async () => {
    const c = await show({ '/api/trading/sessions': [] });
    expect(tile(c, 'Data')).toContain("No session running, so the feed isn't checked");
    expect(tile(c, 'Data')).not.toContain('Feed check');
  });

  it('says the feed check is out of date when readiness cannot be read', async () => {
    const c = await show({ '/api/system/readiness': unavailable() });
    expect(tile(c, 'Data')).toContain('Feed check status is out of date');
    expect(tile(c, 'Data')).not.toContain('Feed check passed');
  });

  it('marks the unrealised column as marked at the last closed bar', async () => {
    const c = await show();
    expect(c.querySelector('th[title="Marked at the last closed bar"]')).not.toBeNull();
    expect(c.textContent).toContain('Unrealised is marked at the last closed bar.');
  });

  it('mounts neither the sessions table nor the equity poll on a phone', async () => {
    const mm = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() });
    vi.stubGlobal('matchMedia', mm);
    try {
      const c = await show();
      expect(c.querySelector('table')).toBeNull();
      expect(calls.some((x) => x.path.includes('equity-curve'))).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
