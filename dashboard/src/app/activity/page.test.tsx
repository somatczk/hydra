import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { click, mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';
import { liveStatus, pendingApproval, platformConfig, session } from '@/test/fixtures';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { default: ActivityPage } = await import('./page');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');
const { actorLabel } = await import('@/components/activity/ActivityLog');

const NOW = Date.parse('2026-10-21T14:00:00Z');

const EVENT = {
  id: 7,
  ts: '2026-10-21T12:00:00Z',
  scope: 'paper',
  kind: 'kill_switch_on',
  tier: null,
  actor: 'user:soma',
  reason: 'kill switch (api)',
  details: { persisted: true },
};

const CLAUDE_EVENT = {
  id: 9,
  ts: '2026-10-21T13:00:00Z',
  scope: 'paper',
  kind: 'approval_requested',
  tier: null,
  actor: 'service:mcp',
  reason: 'drawdown recovered, resume trading',
  details: { approval_id: '3f0b9e2a', action: 'kill_switch_release' },
};

const SERVICE_EVENT = {
  id: 6,
  ts: '2026-10-21T11:00:00Z',
  scope: 'paper',
  kind: 'cb_escalate',
  tier: 2,
  actor: 'risk_engine',
  reason: 'drawdown',
  details: {},
};

const CARRIED = {
  id: 5,
  ts: '2026-10-21T10:00:00Z',
  scope: 'paper',
  kind: 'session_carried',
  tier: null,
  actor: 'risk_engine',
  reason: 'session stopped',
  details: { session_id: 'a1b2c3d4-0000-4000-8000-000000000001', pnl: 12.5, carried: 12.5 },
};

function routes(over: Routes = {}): Routes {
  return {
    '/api/risk/live-status': liveStatus(),
    '/api/trading/sessions': [session()],
    '/api/system/config': platformConfig(),
    '/api/activity/approvals': [],
    '/api/activity?limit=500': [EVENT, CLAUDE_EVENT, SERVICE_EVENT, CARRIED],
    ...over,
  };
}

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

async function show(over: Routes = {}): Promise<HTMLElement> {
  page?.unmount();
  fetchApi.mockImplementation(routeFetch(routes(over), calls));
  page = await mount(
    <ShellStatusProvider>
      <ActivityPage />
    </ShellStatusProvider>,
  );
  return page.container;
}

const chip = (c: HTMLElement, label: string) =>
  Array.from(c.querySelectorAll('button[aria-pressed]')).find((b) => b.textContent === label) as HTMLButtonElement;

const trailText = (c: HTMLElement) => c.querySelectorAll('section')[1]?.textContent ?? '';

beforeEach(() => {
  vi.useFakeTimers({ now: NOW });
  calls = [];
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
  vi.useRealTimers();
});

describe('Activity page', () => {
  it('owns the title, a single h1 and the merged trail newest first', async () => {
    const c = await show();
    expect(c.querySelectorAll('h1')).toHaveLength(1);
    expect(c.querySelector('h1')?.textContent).toBe('Activity');
    const sentences = Array.from(c.querySelectorAll('summary span:nth-child(2)')).map((s) => s.textContent);
    expect(sentences).toEqual([
      'Kill switch turned on',
      'Asked to release the kill switch',
      'Breaker moved to tier 2',
      'Session P&L carried into the ledger',
    ]);
    expect(c.textContent).toContain('by you');
    expect(c.textContent).toContain('by Claude');
    expect(c.textContent).toContain('by engine (automatic)');
  });

  it('filters the trail by actor group with aria-pressed chips', async () => {
    const c = await show();
    expect(chip(c, 'All').getAttribute('aria-pressed')).toBe('true');
    expect(chip(c, 'Claude').getAttribute('aria-pressed')).toBe('false');
    expect(calls.some((x) => x.path.includes('actor='))).toBe(false);

    await click(chip(c, 'Claude'));
    expect(chip(c, 'Claude').getAttribute('aria-pressed')).toBe('true');
    expect(chip(c, 'All').getAttribute('aria-pressed')).toBe('false');
    expect(trailText(c)).toContain('Asked to release the kill switch');
    expect(trailText(c)).not.toContain('Kill switch turned on');

    await click(chip(c, 'Me'));
    expect(trailText(c)).toContain('Kill switch turned on');
    expect(trailText(c)).not.toContain('Asked to release the kill switch');

    await click(chip(c, 'Services'));
    expect(trailText(c)).toContain('Breaker moved to tier 2');
    expect(trailText(c)).not.toContain('Kill switch turned on');
  });

  it('links the row of an event that touched a session', async () => {
    const c = await show();
    const link = c.querySelector(`a[href="/trading/${CARRIED.details.session_id}"]`);
    expect(link?.textContent).toContain('Open session');
  });

  it('links the row of an event that touched a strategy', async () => {
    const STRATEGY_EVENT = {
      id: 8,
      ts: '2026-10-21T12:30:00Z',
      scope: 'paper',
      kind: 'strategy_saved',
      tier: null,
      actor: 'user:soma',
      reason: 'tuned parameters',
      details: { strategy_id: 'sma-cross-btc-1h' },
    };
    const c = await show({ '/api/activity?limit=500': [STRATEGY_EVENT] });
    const link = c.querySelector('a[href="/builder?strategy=sma-cross-btc-1h"]');
    expect(link?.textContent).toContain('Open strategy');
  });

  it('renders the empty state and a per-filter empty line', async () => {
    const c = await show({ '/api/activity?limit=500': [] });
    expect(c.textContent).toContain('No risk events yet. Breaker changes, resets and kill-switch changes appear here.');
    await click(chip(c, 'Claude'));
    expect(c.textContent).toContain('No Claude activity yet.');
  });

  it('shows UNAVAILABLE with the server message when the trail cannot be read', async () => {
    const c = await show({ '/api/activity?limit=500': unavailable('reading activity events') });
    expect(c.textContent).toContain('UNAVAILABLE');
    expect(c.textContent).toContain('reading activity events');
    expect(c.textContent).not.toContain('No risk events yet');
  });

  it('lists pending approvals on top with the ask, the requested time and a Review link', async () => {
    const approval = pendingApproval();
    const c = await show({ '/api/activity/approvals': [approval] });
    const link = c.querySelector(`a[href="/activity/approvals/${approval.id}"]`);
    expect(link?.textContent).toBe('Claude asks to release the kill switch · requested 13:50:04 UTC · Review');
    expect(c.textContent).toContain('Waiting for you');
  });

  it('says no approvals waiting only when the source was read, unknown when it was not', async () => {
    const c = await show();
    expect(c.textContent).toContain('No approvals waiting.');
    const c2 = await show({ '/api/activity/approvals': unavailable() });
    expect(c2.textContent).toContain('Approvals: unknown');
    expect(c2.textContent).not.toContain('No approvals waiting');
  });

  it('labels the MCP actor as Claude everywhere in the trail', () => {
    expect(actorLabel('service:mcp')).toBe('Claude');
  });
});
