import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { advance, click, mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';
import { liveStatus, pendingApproval, platformConfig, session } from '@/test/fixtures';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { ShellStatusProvider, useShellStatus } = await import('./ShellStatusProvider');

/** Renders what the pages read off the shell, so the provider is tested through its real context. */
function Probe() {
  const { needs, approvals, outcome, setOutcome } = useShellStatus();
  return (
    <div>
      <p data-needs-count>{needs.count === null ? '?' : needs.count}</p>
      <ul>
        {needs.items.map((n) => (
          <li key={`${n.kind}:${n.href}`} data-need={n.kind}>
            <a href={n.href}>{n.text}</a>
          </li>
        ))}
      </ul>
      <p data-unavailable>{needs.unavailable.join(',')}</p>
      <p data-approvals-state>{approvals.state}</p>
      <button type="button" onClick={() => setOutcome({ tone: 'caution', title: 'Claimed on', lines: [], actions: [] }, true)}>
        claim
      </button>
      {outcome && (
        <div data-outcome>
          {outcome.outcome.title}
          {outcome.outcome.actions.map((a) => (
            <a key={a.label} href={a.href}>
              {a.label}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

function routes(over: Routes = {}): Routes {
  return {
    '/api/risk/live-status': liveStatus(),
    '/api/trading/sessions': [session()],
    '/api/system/config': platformConfig(),
    '/api/activity/approvals': [],
    ...over,
  };
}

let m: Mounted | null = null;

async function show(over: Routes = {}): Promise<HTMLDivElement> {
  fetchApi.mockImplementation(routeFetch(routes(over), []));
  m = await mount(
    <ShellStatusProvider>
      <Probe />
    </ShellStatusProvider>,
  );
  return m.container;
}

beforeEach(() => {
  fetchApi.mockReset();
});

afterEach(() => {
  m?.unmount();
  m = null;
  vi.useRealTimers();
});

describe('ShellStatusProvider approvals poll', () => {
  it('feeds a pending approval into needsYou, sorted above every other need, with its age', async () => {
    vi.useFakeTimers({ now: Date.parse('2026-10-21T13:52:04Z') });
    const approval = pendingApproval({ requested_at: '2026-10-21T13:50:04+00:00' });
    const c = await show({
      '/api/activity/approvals': [approval],
      '/api/trading/sessions': [session({ status: 'error', error_message: 'feed died' })],
    });
    expect(Array.from(c.querySelectorAll('[data-need]')).map((li) => li.getAttribute('data-need'))).toEqual([
      'approval',
      'error',
    ]);
    expect(c.querySelector('[data-need="approval"] a')?.textContent).toBe(
      'Claude asks to release the kill switch · 2 min ago',
    );
    expect(c.querySelector('[data-need="approval"] a')?.getAttribute('href')).toBe(`/activity/approvals/${approval.id}`);
    expect(c.querySelector('[data-needs-count]')?.textContent).toBe('2');
  });

  it('reads a failed approvals poll as unknown, never as none', async () => {
    const c = await show({ '/api/activity/approvals': unavailable() });
    expect(c.querySelector('[data-needs-count]')?.textContent).toBe('?');
    expect(c.querySelector('[data-unavailable]')?.textContent).toContain('approvals');
    expect(c.querySelectorAll('[data-need]')).toHaveLength(0);
  });

  it('stays quiet when nothing needs you and the approvals poll is ok', async () => {
    const c = await show();
    expect(c.querySelector('[data-needs-count]')?.textContent).toBe('0');
    expect(c.querySelector('[data-approvals-state]')?.textContent).toBe('ok');
  });
});

describe('ShellStatusProvider outcome', () => {
  it('links the kill-switch disagreement outcome to /activity', async () => {
    vi.useFakeTimers({ now: Date.parse('2026-10-21T14:00:00Z') });
    const c = await show();
    await click(c.querySelector('button') as HTMLButtonElement);
    expect(c.querySelector('[data-outcome]')?.textContent).toContain('Claimed on');
    await advance(10_000);
    const outcome = c.querySelector('[data-outcome]');
    expect(outcome?.textContent).toContain('The engine now reports the kill switch as off.');
    expect(outcome?.querySelector('a')?.getAttribute('href')).toBe('/activity');
  });
});
