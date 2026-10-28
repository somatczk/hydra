import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import {
  advance,
  buttonByText,
  click,
  dialogPolyfill,
  inputByLabel,
  mount,
  routeFetch,
  typeInto,
  type Mounted,
  type Mutations,
  type Routes,
} from '@/test/pageHarness';
import { approvalDetail, liveStatus, pendingApproval, platformConfig, session } from '@/test/fixtures';
import type { ApprovalDetail } from '@/lib/derive';

const ID = '3f0b9e2a-1000-4000-8000-000000000001';
const NOW = Date.parse('2026-10-21T13:55:00Z');

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('next/navigation', () => ({
  useParams: () => ({ id: ID }),
  usePathname: () => `/activity/approvals/${ID}`,
  useRouter: () => ({ push: vi.fn() }),
}));

const { default: ApprovalReviewPage } = await import('./page');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');

const CLAUDE_EVENTS = [
  {
    id: 3,
    ts: '2026-10-21T13:40:00Z',
    scope: 'paper',
    kind: 'approval_requested',
    tier: null,
    actor: 'service:mcp',
    reason: 'drawdown recovered, resume trading',
    details: { approval_id: ID, action: 'kill_switch_release' },
  },
  {
    id: 2,
    ts: '2026-10-21T13:30:00Z',
    scope: 'paper',
    kind: 'kill_switch_on',
    tier: null,
    actor: 'service:mcp',
    reason: 'drawdown is too deep',
    details: { persisted: true },
  },
];

const envelope = (status: number, code: string, message: string, state: string, extra: Record<string, unknown> = {}) =>
  new ApiError(status, JSON.stringify({ detail: { code, message, state, ...extra } }));

let m: Mounted;
let calls: Array<{ path: string; init?: RequestInit }>;
/** The detail the route serves; flipped by the tests that decide, like the engine's store would. */
let detail: ApprovalDetail;

function setup(d: ApprovalDetail, mutations: Mutations = {}, over: Routes = {}) {
  detail = d;
  fetchApi.mockImplementation(
    routeFetch(
      {
        [`/api/activity/approvals/${ID}`]: () => detail,
        '/api/activity?actor=service:mcp&limit=10': CLAUDE_EVENTS,
        '/api/risk/live-status': liveStatus(),
        '/api/trading/sessions': [session()],
        '/api/system/config': platformConfig(),
        '/api/activity/approvals': [pendingApproval()],
        ...over,
      },
      calls,
      mutations,
    ),
  );
}

const page = () => mount(<ShellStatusProvider><ApprovalReviewPage /></ShellStatusProvider>);
const text = () => m.container.textContent ?? '';
const sent = (method: string, path: string) =>
  calls.filter((c) => c.path === path && c.init?.method === method).map((c) => JSON.parse(String(c.init?.body)));

beforeAll(dialogPolyfill);

beforeEach(() => {
  vi.useFakeTimers({ now: NOW });
  calls = [];
  fetchApi.mockReset();
});

afterEach(() => {
  m.unmount();
  vi.useRealTimers();
  fetchApi.mockReset();
});

const typeCode = async (value: string) => typeInto(inputByLabel(m.container, 'One-time code'), value);
const primary = () => buttonByText(m.container, 'Approve and release the kill switch');
const deny = () => buttonByText(m.container, 'Deny');

describe('approval review: the ask', () => {
  it('shows What, Why, Context, Claude\'s trail and the static expiry', async () => {
    setup(approvalDetail());
    m = await page();
    expect(m.container.querySelectorAll('h1')).toHaveLength(1);
    expect(m.container.querySelector('h1')?.textContent).toBe('Claude asks to release the kill switch');
    expect(text()).toContain('Claude wrote: “drawdown recovered, resume trading”');
    expect(text()).toContain('Action');
    expect(text()).toContain('kill_switch_release');
    expect(text()).toContain('Params');
    expect(text()).toContain('{"reason":"drawdown recovered, resume trading"}');
    expect(text()).toContain("Claude's recent activity");
    expect(text()).toContain('Asked to release the kill switch');
    expect(text()).toContain('Kill switch turned on');
    expect(text()).toContain('Expires in 5 min');
  });

  it('re-renders the expiry at most every 10 s and never as a live region', async () => {
    setup(approvalDetail());
    m = await page();
    const expiry = () => Array.from(m.container.querySelectorAll('p')).find((p) => p.textContent?.startsWith('Expires in'));
    expect(expiry()?.textContent).toBe('Expires in 5 min');
    expect(expiry()?.getAttribute('aria-live')).toBeNull();
    expect(expiry()?.getAttribute('role')).toBeNull();
    await advance(10_000);
    expect(expiry()?.textContent).toBe('Expires in 4 min');
  });

  it('states what releasing does before asking for a decision: the C-REL-1 facts and the still-active tier', async () => {
    setup(approvalDetail(), {}, {
      '/api/risk/live-status': liveStatus({
        circuit_breaker_restrictions: {
          active_tier: 2,
          can_open_new_trades: false,
          size_multiplier: 0.5,
          must_flatten: false,
          is_shutdown: false,
          daily_halt_until: null,
          consecutive_pause_until: null,
        },
      }),
    });
    m = await page();
    expect(text()).toContain("Releasing lets sessions start and open positions again. It doesn't restart stopped sessions.");
    expect(text()).toContain('Breaker tier 2 is still active and still blocks entries; reset it separately.');
    expect(text()).toContain('Action');
    expect(text()).toContain('kill_switch_release');
    expect(text()).toContain('Params');
  });

  it('renders no release facts for another gated action', async () => {
    setup(approvalDetail({ action: 'start_session', params: { strategy_id: 'saw' } }));
    m = await page();
    expect(text()).not.toContain('Releasing lets sessions start');
    expect(text()).not.toContain("It doesn't restart stopped sessions.");
  });
});

describe('approval review: your own challenge (must-fix 7, A2≡S1)', () => {
  const USER_EVENT = {
    id: 4,
    ts: '2026-10-21T13:45:00Z',
    scope: 'live',
    kind: 'approval_requested',
    tier: null,
    actor: 'user:soma',
    reason: 'Manual live order: buy 0.0015 BTC in SMA cross',
    details: { approval_id: ID, action: 'live_order' },
  };

  it('reads a user-initiated live order as your own ask throughout, never Claude phrasing', async () => {
    setup(
      approvalDetail({
        actor: 'user:soma',
        action: 'live_order',
        reason: 'Manual live order: buy 0.0015 BTC in SMA cross',
        params: { session_id: 'a1b2c3d4-0000-4000-8000-000000000001', symbol: 'BTCUSDT', side: 'buy', quantity: '0.0015' },
      }),
      {},
      { '/api/activity?actor=user:soma&limit=10': [USER_EVENT] },
    );
    m = await page();
    expect(m.container.querySelector('h1')?.textContent).toBe('You asked to place a live order');
    expect(text()).toContain('You wrote: “Manual live order: buy 0.0015 BTC in SMA cross”');
    expect(text()).toContain('Your recent activity');
    expect(text()).toContain('The 6 digits shown to you. Single use.');
    expect(text()).toContain('Approving runs it now, recorded as approved by you.');
    expect(text()).not.toContain('Claude asks to');
    expect(text()).not.toContain("Claude's recent activity");
    expect(text()).not.toContain('Claude wrote');
    expect(text()).not.toContain('The 6 digits Claude shows you');
    expect(text()).not.toContain('Approving runs it as Claude');
    expect(calls.some((c) => c.path === '/api/activity?actor=user:soma&limit=10')).toBe(true);
    expect(calls.some((c) => c.path === '/api/activity?actor=service:mcp&limit=10')).toBe(false);
  });
});

describe('approval review: the one-time code', () => {
  it('cannot submit without a full code and enables the primary only at 6 digits', async () => {
    setup(approvalDetail());
    m = await page();
    const field = inputByLabel(m.container, 'One-time code');
    expect(field.getAttribute('autocomplete')).toBe('one-time-code');
    expect(field.getAttribute('inputmode')).toBe('numeric');
    expect(field.getAttribute('maxlength')).toBe('6');
    expect(primary().disabled).toBe(true);
    await typeCode('12345');
    expect(primary().disabled).toBe(true);
    await typeCode('123456');
    expect(primary().disabled).toBe(false);
    expect(inputByLabel(m.container, 'One-time code').value).toBe('123456');
  });

  it('keeps only digits and at most 6', async () => {
    setup(approvalDetail());
    m = await page();
    await typeCode('12a34b5678');
    expect(inputByLabel(m.container, 'One-time code').value).toBe('123456');
  });

  it('sends exactly the typed code and records the double attribution', async () => {
    setup(approvalDetail(), {
      [`POST /api/activity/approvals/${ID}/approve`]: () => {
        detail = approvalDetail({ status: 'approved', approved_by: 'user:soma', decided_at: '2026-10-21T13:56:00+00:00' });
        return { executed: true, result: { released: true }, actor: 'service:mcp', approved_by: 'user:soma' };
      },
    });
    m = await page();
    await typeCode('424242');
    await click(primary());
    expect(sent('POST', `/api/activity/approvals/${ID}/approve`)).toEqual([{ code: '424242' }]);
    expect(text()).toContain('Released by Claude, approved by you.');
    expect(text()).toContain('You approved this at 13:56:00 UTC.');
    expect(text()).not.toContain('on another device');
    expect(text()).not.toContain('One-time code');
  });

  it('focuses the primary only once the code is valid; Deny stays reachable without one (S11)', async () => {
    setup(approvalDetail());
    m = await page();
    const field = inputByLabel(m.container, 'One-time code');
    await typeInto(field, '12345');
    expect(document.activeElement).not.toBe(primary());
    expect(deny().disabled).toBe(false);
    expect(deny().tabIndex).toBeGreaterThanOrEqual(0);
    await typeInto(field, '123456');
    expect(document.activeElement).toBe(primary());
  });

  it('keeps the typed values and says nothing changed on a wrong code, with the attempts left', async () => {
    setup(approvalDetail(), {
      [`POST /api/activity/approvals/${ID}/approve`]: envelope(422, 'code_mismatch', "That code didn't match. Nothing changed.", 'invalid', { attempts_left: 4 }),
    });
    m = await page();
    await typeCode('000000');
    await click(primary());
    const d = m.container;
    expect(inputByLabel(d, 'One-time code').value).toBe('000000');
    expect(d.textContent).toContain("That code didn't match. Nothing changed.");
    expect(d.textContent).toContain('4 attempts left.');
    expect(primary().disabled).toBe(false);
    expect(sent('POST', `/api/activity/approvals/${ID}/approve`)).toHaveLength(1);
  });

  it('shows the server message inline when this principal may not decide', async () => {
    setup(approvalDetail(), {
      [`POST /api/activity/approvals/${ID}/approve`]: envelope(403, 'principal_not_allowed', 'Only a signed-in person can approve or deny; service principals cannot', 'forbidden'),
    });
    m = await page();
    await typeCode('424242');
    await click(primary());
    expect(m.container.textContent).toContain('Only a signed-in person can approve or deny; service principals cannot');
    expect(inputByLabel(m.container, 'One-time code').value).toBe('424242');
  });

  it('returns focus to the code field after a wrong code, so it can be edited (A3)', async () => {
    setup(approvalDetail(), {
      [`POST /api/activity/approvals/${ID}/approve`]: envelope(422, 'code_mismatch', "That code didn't match. Nothing changed.", 'invalid', { attempts_left: 4 }),
    });
    m = await page();
    await typeCode('000000');
    expect(document.activeElement).toBe(primary());
    await click(primary());
    expect(document.activeElement).toBe(inputByLabel(m.container, 'One-time code'));
  });

  it('a 409 executor_not_registered is its own outcome: the state and the server message, never an outage (A1)', async () => {
    setup(approvalDetail(), {
      [`POST /api/activity/approvals/${ID}/approve`]: envelope(
        409,
        'executor_not_registered',
        "No executor is registered for 'kill_switch_release'; the approval stays pending and the code was not used",
        'invalid',
        { action: 'kill_switch_release' },
      ),
    });
    m = await page();
    await typeCode('424242');
    await click(primary());
    expect(text()).toContain('The engine cannot execute this action yet.');
    expect(text()).toContain("No executor is registered for 'kill_switch_release'; the approval stays pending and the code was not used");
    expect(text()).not.toContain("couldn't reach the engine");
    expect(text()).toContain('One-time code');
  });

  it('a failed live order renders the unknown-live copy, never "Ask for a new one." (R24 caller half)', async () => {
    const msg = "The approved action 'live_order' did not run: exchange unreachable";
    setup(approvalDetail({ action: 'live_order', params: { session_id: 'a1b2c3d4-0000-4000-8000-000000000001', symbol: 'BTCUSDT', side: 'buy', quantity: '0.0015' } }), {
      [`POST /api/activity/approvals/${ID}/approve`]: envelope(503, 'approval_execution_failed', msg, 'unavailable'),
    });
    m = await page();
    expect(m.container.querySelector('h1')?.textContent).toBe('Claude asks to place a live order');
    await typeCode('424242');
    await click(buttonByText(m.container, 'Approve and place a live order'));
    expect(text()).toContain('Not confirmed.');
    expect(text()).toContain("We don't know whether the order was placed. Check this session's fills and positions before trying again.");
    expect(text()).toContain(msg);
    expect(text()).not.toContain('Ask for a new one.');
  });
});

describe('approval review: a lost answer', () => {
  it('credits this device when a retry after a lost answer meets 409 already_decided (A4)', async () => {
    setup(approvalDetail(), {
      [`POST /api/activity/approvals/${ID}/approve`]: (n: number) =>
        n === 1
          ? new TypeError('Failed to fetch')
          : envelope(409, 'already_decided', 'This approval was already approved', 'invalid'),
    });
    m = await page();
    await typeCode('424242');
    await click(primary());
    expect(text()).toContain('Not confirmed.');
    detail = approvalDetail({ status: 'approved', approved_by: 'user:soma', decided_at: '2026-10-21T13:56:00+00:00' });
    await click(buttonByText(m.container, 'Try again'));
    expect(sent('POST', `/api/activity/approvals/${ID}/approve`)).toHaveLength(2);
    expect(text()).toContain('You approved this at 13:56:00 UTC.');
    expect(text()).not.toContain('on another device');
  });
});

describe('approval review: deny', () => {
  it('denies without a code and says Claude can ask again', async () => {
    setup(approvalDetail(), {
      [`POST /api/activity/approvals/${ID}/deny`]: () => {
        detail = approvalDetail({ status: 'denied', decided_at: '2026-10-21T13:56:00+00:00' });
        return { id: ID, status: 'denied' };
      },
    });
    m = await page();
    await click(deny());
    expect(sent('POST', `/api/activity/approvals/${ID}/deny`)).toEqual([{}]);
    expect(text()).toContain('Denied.');
    expect(text()).toContain('Claude can ask again.');
    expect(text()).toContain('You denied this at 13:56:00 UTC.');
    expect(text()).not.toContain('One-time code');
  });

  it('disables Approve while a Deny is in flight (A2)', async () => {
    setup(approvalDetail(), {
      [`POST /api/activity/approvals/${ID}/deny`]: () => new Promise(() => {}),
    });
    m = await page();
    await typeCode('424242');
    expect(primary().disabled).toBe(false);
    await click(deny());
    expect(primary().disabled).toBe(true);
    expect(deny().disabled).toBe(true);
  });

  it('a network-failed deny says the denial is unknown and never mentions a code (A5)', async () => {
    setup(approvalDetail(), {
      [`POST /api/activity/approvals/${ID}/deny`]: new TypeError('Failed to fetch'),
    });
    m = await page();
    await click(deny());
    expect(text()).toContain("We couldn't reach the engine, so we don't know whether the denial was recorded");
    expect(text()).not.toContain('the code');
  });
});

describe('approval review: states that need no code', () => {
  it('says the request expired and that nothing changed', async () => {
    setup(approvalDetail({ status: 'expired', expires_at: '2026-10-21T13:40:04+00:00', decided_at: '2026-10-21T13:40:04+00:00' }));
    m = await page();
    expect(text()).toContain('This request expired at 13:40:04 UTC. Nothing changed.');
    expect(text()).not.toContain('One-time code');
    expect(() => deny()).toThrow();
  });

  it('says who decided it and where, for both decisions', async () => {
    setup(approvalDetail({ status: 'approved', approved_by: 'user:soma', decided_at: '2026-10-21T13:52:00+00:00' }));
    m = await page();
    expect(text()).toContain('You approved this at 13:52:00 UTC on another device.');
    m.unmount();
    setup(approvalDetail({ status: 'denied', decided_at: '2026-10-21T13:52:00+00:00' }));
    m = await page();
    expect(text()).toContain('You denied this at 13:52:00 UTC on another device.');
  });

  it('says a failed approval was invalidated, with the engine error verbatim', async () => {
    setup(approvalDetail({ status: 'failed', error: 'executor raised', decided_at: '2026-10-21T13:54:00+00:00' }));
    m = await page();
    expect(text()).toContain('This approval was invalidated: too many wrong codes, or the action failed to run. Ask for a new one.');
    expect(text()).toContain('executor raised');
  });

  it('a failed live order never says "Ask for a new one." — the order may exist unwatched (R24)', async () => {
    setup(approvalDetail({ status: 'failed', action: 'live_order', error: 'exchange unreachable', decided_at: '2026-10-21T13:54:00+00:00' }));
    m = await page();
    expect(text()).toContain('This approval was invalidated: too many wrong codes, or the action failed to run.');
    expect(text()).toContain("We don't know whether the order was placed. Check this session's fills and positions before trying again.");
    expect(text()).not.toContain('Ask for a new one.');
  });

  it('shows a decision that happened elsewhere after the POST, instead of an error', async () => {
    setup(approvalDetail(), {
      [`POST /api/activity/approvals/${ID}/approve`]: envelope(409, 'already_decided', 'This approval was already approved', 'invalid'),
    });
    m = await page();
    await typeCode('424242');
    detail = approvalDetail({ status: 'approved', approved_by: 'user:soma', decided_at: '2026-10-21T13:56:00+00:00' });
    await click(primary());
    expect(text()).toContain('You approved this at 13:56:00 UTC on another device.');
    expect(text()).not.toContain('already approved');
  });
});

describe('approval review: loading and missing approvals', () => {
  it('says no such approval and why the engine may not know it', async () => {
    setup(approvalDetail());
    m = await page();
    m.unmount();
    fetchApi.mockImplementation(
      routeFetch(
        {
          [`/api/activity/approvals/${ID}`]: new ApiError(404, JSON.stringify({ detail: { code: 'approval_not_found', message: 'No such approval', state: 'not_found' } })),
          '/api/activity?actor=service:mcp&limit=10': [],
          '/api/risk/live-status': liveStatus(),
          '/api/trading/sessions': [session()],
          '/api/system/config': platformConfig(),
        },
        calls,
      ),
    );
    m = await page();
    expect(text()).toContain('No such approval');
    expect(text()).toContain('a restart drops them');
    expect(m.container.querySelector('a[href="/activity"]')).not.toBeNull();
  });

  it('shows UNAVAILABLE with a retry when the detail cannot be read', async () => {
    setup(approvalDetail());
    m = await page();
    m.unmount();
    fetchApi.mockImplementation(
      routeFetch(
        {
          [`/api/activity/approvals/${ID}`]: new ApiError(503, JSON.stringify({ detail: { code: 'database_unavailable', message: 'Database is not available', state: 'unavailable' } })),
          '/api/activity?actor=service:mcp&limit=10': [],
          '/api/risk/live-status': liveStatus(),
          '/api/trading/sessions': [session()],
          '/api/system/config': platformConfig(),
        },
        calls,
      ),
    );
    m = await page();
    expect(text()).toContain('UNAVAILABLE');
    expect(text()).toContain('Database is not available');
    expect(buttonByText(m.container, 'Retry')).toBeTruthy();
  });
});
