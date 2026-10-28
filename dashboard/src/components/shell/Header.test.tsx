import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { advance, mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';
import { liveSession, liveStatus, platformConfig, session } from '@/test/fixtures';

vi.mock('next/navigation', () => ({ usePathname: () => '/trading/abc' }));
const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { AppLayout } = await import('@/components/layout/AppLayout');
const { ThemeProvider } = await import('@/components/layout/ThemeProvider');

let m: Mounted;
let calls: Array<{ path: string }>;

function setup(routes: Routes = {}) {
  fetchApi.mockImplementation(
    routeFetch(
      {
        '/api/risk/live-status': liveStatus({ running_sessions: 2 }),
        '/api/trading/sessions': [session(), session({ session_id: 's2' })],
        '/api/system/config': platformConfig(),
        ...routes,
      },
      calls,
    ),
  );
}

const shell = () =>
  mount(
    <ThemeProvider>
      <AppLayout>
        <h1>Sessions</h1>
      </AppLayout>
    </ThemeProvider>,
  );

const header = () => m.container.querySelector('header')?.textContent ?? '';
const conditions = () => m.container.querySelector('[role="status"][aria-label="Conditions"]');

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  m.unmount();
  vi.useRealTimers();
  fetchApi.mockReset();
});

describe('Header', () => {
  it('renders the strip in order, with the destination as the app-bar title and no h1 of its own', async () => {
    setup();
    m = await shell();
    const h = header();
    expect(h).toContain('PAPER');
    expect(h).toContain('Kill switch off');
    expect(h).toContain('Tier 0 · entries allowed');
    expect(h).toContain('2 sessions running');
    expect(h).toContain('Nothing needs you');
    expect(h.indexOf('PAPER')).toBeLessThan(h.indexOf('Kill switch off'));
    expect(h.indexOf('Kill switch off')).toBeLessThan(h.indexOf('Tier 0'));
    expect(h.indexOf('Tier 0')).toBeLessThan(h.indexOf('2 sessions running'));
    expect(h.indexOf('2 sessions running')).toBeLessThan(h.indexOf('Fetched'));
    expect(h.indexOf('Fetched')).toBeLessThan(h.indexOf('Nothing needs you'));
    expect(h.startsWith('Sessions')).toBe(true);
    expect(m.container.querySelectorAll('h1')).toHaveLength(1);
    const links = Array.from(m.container.querySelectorAll('header a')).map((a) => a.getAttribute('href'));
    expect(links).toEqual(expect.arrayContaining(['/risk#breakers', '/trading', '/#needs-you']));
  });

  it('never shows 0 when running_sessions is null', async () => {
    setup({ '/api/risk/live-status': liveStatus({ running_sessions: null }) });
    m = await shell();
    expect(header()).toContain('Sessions UNKNOWN');
    expect(header()).not.toMatch(/\b0 sessions?\b|Nothing running/);
  });

  it('marks the strip STALE with "as of" after live-status fails twice, keeping the last values', async () => {
    vi.useFakeTimers();
    let failing = false;
    setup({ '/api/risk/live-status': () => (failing ? unavailable() : liveStatus({ running_sessions: 2 })) });
    m = await shell();
    expect(header()).not.toContain('STALE');
    failing = true;
    await advance(10_000);
    expect(header()).not.toContain('STALE');
    await advance(10_000);
    expect(header()).toContain('STALE');
    expect(header()).toContain('as of');
    expect(header()).toContain('2 sessions running');
    expect(conditions()?.textContent).toContain('Data STALE · as of');
  });

  it('does not re-render ages faster than every 10 s', async () => {
    vi.useFakeTimers();
    setup();
    m = await shell();
    const before = header();
    expect(before).toContain('Fetched 0 s ago');
    await advance(9_000);
    expect(header()).toBe(before);
  });

  it('mounts the condition bar and outcome regions empty, and shows the bar only when abnormal', async () => {
    setup();
    m = await shell();
    expect(conditions()).not.toBeNull();
    expect(conditions()?.textContent).toBe('');
    expect(m.container.querySelector('[role="status"][aria-label="Outcome"]')?.textContent).toBe('');
    expect(m.container.querySelector('[role="alert"][aria-label="Outcome alert"]')?.textContent).toBe('');
    m.unmount();

    setup({
      '/api/risk/live-status': liveStatus({
        circuit_breaker_restrictions: { ...liveStatus().circuit_breaker_restrictions!, active_tier: 1 },
      }),
    });
    m = await shell();
    expect(conditions()?.textContent).toContain('Breaker tier 1 · entries allowed');
    expect(conditions()?.querySelector('a')?.getAttribute('href')).toBe('/risk#breakers');
  });

  it('counts current conditions in Needs you and says "?" when a source fails', async () => {
    setup({
      '/api/trading/sessions': [session({ status: 'stopped', positions_at_stop: 1 })],
    });
    m = await shell();
    expect(header()).toContain('Needs you: 1');
    expect(conditions()?.textContent).toContain('1 thing needs you');
    m.unmount();

    setup({ '/api/system/config': unavailable() });
    m = await shell();
    expect(header()).toContain('Needs you: ?');
    expect(header()).toContain('PAPER');
  });

  it('ignores the stored config mode entirely: config live, gate off, no live session -> PAPER (case 11 reworked)', async () => {
    setup({ '/api/system/config': platformConfig({ trading_mode: 'live' }) });
    m = await shell();
    expect(header()).toContain('PAPER');
    expect(header()).not.toContain('Stored mode is live');
    expect(header()).not.toContain('LIVE');
  });

  it('shows LIVE with real money when the gate is armed, before any session runs', async () => {
    setup({ '/api/risk/live-status': liveStatus({ live_armed: true }) });
    m = await shell();
    expect(header()).toContain('LIVE');
    expect(header()).toContain('real money');
    expect(header()).not.toContain('PAPER');
  });

  it('shows LIVE over a running live session even while the config says paper (the safety reading)', async () => {
    setup({ '/api/trading/sessions': [liveSession()] });
    m = await shell();
    expect(header()).toContain('LIVE');
    expect(header()).not.toContain('PAPER');
  });

  it('keeps LIVE over a stopped live session still holding real positions (R23)', async () => {
    setup({
      '/api/trading/sessions': [liveSession({ session_id: 'h1', status: 'stopped', positions_at_stop: 1 })],
    });
    m = await shell();
    expect(header()).toContain('LIVE');
    expect(header()).not.toContain('PAPER');
  });

  it('renders "?" and never PAPER while the live status cannot be read (R23, fail-closed)', async () => {
    setup({ '/api/risk/live-status': unavailable() });
    m = await shell();
    expect(header()).toContain('Trading mode unknown');
    expect(header()).not.toContain('PAPER');
  });

  it('polls live-status, sessions, config and pending approvals from the shell only', async () => {
    setup();
    m = await shell();
    expect(calls.map((c) => c.path).sort()).toEqual([
      '/api/activity/approvals',
      '/api/risk/live-status',
      '/api/system/config',
      '/api/trading/sessions',
    ]);
  });
});
