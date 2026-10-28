import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import {
  buttonByText,
  click,
  dialogPolyfill,
  inputByLabel,
  mount,
  routeFetch,
  typeInto,
  unavailable,
  type Mounted,
  type Mutations,
  type Routes,
} from '@/test/pageHarness';
import { disarmResponse, liveChallenge, liveSession, liveStatus, session } from '@/test/fixtures';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('next/navigation', () => ({ usePathname: () => '/settings' }));

const { LiveGate } = await import('./LiveGate');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');

const APPROVE_PATH = '/api/activity/approvals/3f0b9e2a-2000-4000-8000-000000000001/approve';
const R19 =
  "Live sessions place real orders on Binance with real money. Paper sessions keep running. Every live order you place needs the one-time code; once a live session starts, its strategy trades under that start's authorization — bounded by the kill switch, risk limits and exchange stops.";

/** The shared envelope from `api_error`: extras sit beside code/message/state. */
const envelope = (status: number, code: string, message: string, state: string, extra: Record<string, unknown> = {}) =>
  new ApiError(status, JSON.stringify({ detail: { code, message, state, ...extra } }));

let m: Mounted;
let calls: Array<{ path: string; init?: RequestInit }>;
let armed: boolean;
let list: Array<ReturnType<typeof session>>;

/** One mock per test: the live-status route reads `armed` at call time so a leg can flip it. */
function begin(routes: Routes = {}, mutations: Mutations = {}) {
  fetchApi.mockImplementation(
    routeFetch(
      {
        '/api/risk/live-status': () => liveStatus({ live_armed: armed }),
        '/api/trading/sessions': () => list,
        '/api/system/config': { trading_mode: 'paper', paper_capital: 10000, paper_capital_pending: null },
        ...routes,
      },
      calls,
      { 'POST /api/live/arm': liveChallenge({ params: { live_capital: 25000 } }), ...mutations },
    ),
  );
  return mount(<ShellStatusProvider><LiveGate /></ShellStatusProvider>);
}

const gate = begin;
const text = () => m.container.textContent ?? '';
/** The mounted section only — the two closed ceremony dialogs also sit in the DOM. */
const sectionNode = () =>
  Array.from(m.container.querySelectorAll('h2')).find((h) => h.textContent === 'Live trading')?.closest('section') ?? m.container;
const section = () => sectionNode().textContent ?? '';
const dialog = () => m.container.querySelector('dialog[open]') as HTMLDialogElement;
const onButton = () => buttonByText(m.container, 'Turn on live trading…');
const offButton = () => buttonByText(m.container, 'Turn off live trading…');

/** Opens the arm ceremony and drives it to the code leg. */
async function driveToCode(mutations: Mutations = {}) {
  m = await gate({}, mutations);
  await click(onButton());
  await typeInto(inputByLabel(dialog(), 'Live capital (USDT)'), '25000');
  await click(buttonByText(dialog(), 'Turn on live trading'));
}

beforeEach(() => {
  dialogPolyfill();
  calls = [];
  armed = false;
  list = [];
});

afterEach(() => {
  m.unmount();
  fetchApi.mockReset();
});

describe('LiveGate', () => {
  it('shows the paper state with the arm door and no live claim', async () => {
    m = await gate();
    expect(section()).toContain('Paper. Every live order will need a one-time code.');
    expect(onButton()).toBeTruthy();
    expect(section()).not.toContain('Live trading is on.');
    expect(section()).not.toMatch(/\bLIVE\b/);
    expect(section()).not.toContain('real money');
  });

  it('arms through the L4 ceremony: capital → the engine code shown in-flow → typed back → on', async () => {
    m = await gate({}, {
      [`POST ${APPROVE_PATH}`]: () => {
        armed = true;
        return { executed: true, result: { live_armed: true }, actor: 'user:soma', approved_by: 'user:soma' };
      },
    });
    await click(onButton());
    const d = dialog();
    expect(d.textContent).toContain(R19);
    const primary = buttonByText(d, 'Turn on live trading');
    expect(primary.disabled).toBe(true);
    await typeInto(inputByLabel(d, 'Live capital (USDT)'), '25000');
    await click(primary);
    const armCall = calls.find((c) => c.path === '/api/live/arm');
    expect(JSON.parse(armCall?.init?.body as string)).toEqual({
      live_capital: 25000,
      reason: 'Live trading armed from Settings',
    });
    expect(d.textContent).toContain('482913');
    const codeInput = inputByLabel(d, 'One-time code');
    expect(codeInput.autocomplete).toBe('one-time-code');
    expect(codeInput.inputMode).toBe('numeric');
    expect(buttonByText(d, 'Turn on live trading').disabled).toBe(true);
    await typeInto(codeInput, '482913');
    await click(buttonByText(d, 'Turn on live trading'));
    expect(calls.filter((c) => c.path === APPROVE_PATH).map((c) => JSON.parse(c.init?.body as string))).toEqual([
      { code: '482913' },
    ]);
    expect(text()).toContain('Live trading is on.');
    expect(text()).toContain('Each live session start and manual live order still needs its own one-time code.');
    expect(offButton()).toBeTruthy();
    expect(m.container.querySelector('dialog[open]')).toBeNull();
  });

  it('keeps a wrong code inline with focus back on the field, and the code kept', async () => {
    await driveToCode({ [`POST ${APPROVE_PATH}`]: envelope(422, 'code_mismatch', "That code didn't match. Nothing changed.", 'invalid', { attempts_left: 4 }) });
    const d = dialog();
    await typeInto(inputByLabel(d, 'One-time code'), '482913');
    await click(buttonByText(d, 'Turn on live trading'));
    expect(d.textContent).toContain("That code didn't match. Nothing changed. 4 attempts left.");
    expect(inputByLabel(d, 'One-time code').value).toBe('482913');
    expect(document.activeElement).toBe(inputByLabel(d, 'One-time code'));
    expect(text()).not.toContain('Live trading is on.');
  });

  it('states an expired code the /activity way and requires a fresh challenge', async () => {
    await driveToCode({ [`POST ${APPROVE_PATH}`]: envelope(409, 'approval_expired', 'This approval has expired', 'invalid') });
    const d = dialog();
    await typeInto(inputByLabel(d, 'One-time code'), '482913');
    await click(buttonByText(d, 'Turn on live trading'));
    expect(d.textContent).toMatch(/The one-time code expired at .* UTC\. Nothing changed\./);
    expect(d.textContent).toContain('Turn it on again for a fresh code.');
    expect(inputByLabel(d, 'Live capital (USDT)')).toBeTruthy();
    await click(buttonByText(d, 'Turn on live trading'));
    expect(calls.filter((c) => c.path === '/api/live/arm')).toHaveLength(2);
  });

  it('names the still-running live sessions when turning off, and keeps them visible after (R10)', async () => {
    armed = true;
    list = [
      liveSession({ session_id: 'live-1', strategy_name: 'Live momentum' }),
      liveSession({ session_id: 'live-2', strategy_name: 'Live revert' }),
    ];
    m = await gate({}, {
      'POST /api/live/disarm': () => {
        armed = false;
        return disarmResponse({ running_live_sessions: ['live-1', 'live-2'], running_live_sessions_count: 2 });
      },
    });
    expect(text()).toContain('Live trading is on.');
    await click(offButton());
    const d = dialog();
    expect(d.textContent).toContain("2 live sessions keep running under their start's authorization");
    expect(d.textContent).toContain('Live momentum');
    expect(d.textContent).toContain('Live revert');
    expect(d.querySelectorAll('a[href="/trading/live-1"], a[href="/trading/live-2"]')).toHaveLength(4);
    await click(buttonByText(d, 'Turn off live trading'));
    expect(calls.filter((c) => c.path === '/api/live/disarm').map((c) => JSON.parse(c.init?.body as string))).toEqual([
      { reason: 'Live trading turned off from Settings' },
    ]);
    expect(text()).toContain('Paper. Every live order will need a one-time code.');
    expect(section()).toContain('Live momentum');
    expect(section()).toContain('Live revert');
    expect(sectionNode().querySelectorAll('a[href="/trading/live-1"]')).toHaveLength(2);
  });

  it('still names the sessions R10 confirmed while the session list cannot be read', async () => {
    armed = true;
    m = await gate(
      { '/api/trading/sessions': unavailable('sessions unreadable') },
      {
        'POST /api/live/disarm': () => {
          armed = false;
          return disarmResponse({ running_live_sessions: ['live-1', 'live-2'], running_live_sessions_count: 2 });
        },
      },
    );
    await click(offButton());
    expect(dialog().textContent).toContain('No live session is running.');
    await click(buttonByText(dialog(), 'Turn off live trading'));
    expect(text()).toContain('2 live sessions keep running');
    expect(text()).toContain('live-1');
    expect(text()).toContain('live-2');
  });

  it('never hides a running live session in the paper state, with its Stop link', async () => {
    list = [liveSession({ strategy_name: 'Live momentum' })];
    m = await gate();
    expect(section()).toContain('Paper. Every live order will need a one-time code.');
    expect(section()).toContain("1 live session keeps running under its start's authorization");
    expect(section()).toContain('Live momentum');
    const links = Array.from(sectionNode().querySelectorAll('a[href="/trading/a1b2c3d4-0000-4000-8000-000000000001"]')).map(
      (a) => a.textContent,
    );
    expect(links).toEqual(['Live momentum', 'Stop']);
  });

  it('hides every door while the live status cannot be read', async () => {
    m = await gate({ '/api/risk/live-status': unavailable('risk state unavailable') });
    expect(text()).toContain('Live gate state unknown.');
    expect(text()).not.toContain('Turn on live trading…');
    expect(text()).not.toContain('Turn off live trading…');
  });

  it('hides the on-button after the code service is down, with an honest recovery probe', async () => {
    m = await gate({}, { 'POST /api/live/arm': unavailable('approval store unavailable') });
    await click(onButton());
    await typeInto(inputByLabel(dialog(), 'Live capital (USDT)'), '25000');
    await click(buttonByText(dialog(), 'Turn on live trading'));
    expect(dialog().textContent).toContain("Live trading wasn't turned on.");
    expect(dialog().textContent).toContain('Live actions are hidden until the code service recovers.');
    await click(buttonByText(dialog(), 'Cancel'));
    expect(text()).toContain("Live trading wasn't turned on.");
    expect(text()).toContain('Live actions are hidden until the code service recovers.');
    expect(text()).not.toContain('Turn on live trading…');
    await click(buttonByText(m.container, 'Try again'));
    expect(text()).toContain('Turn on live trading…');
  });

  it('shows a definite credential refusal verbatim and keeps the door', async () => {
    m = await gate({}, {
      'POST /api/live/arm': envelope(409, 'credentials_not_live_capable', 'The stored Binance keys can withdraw; connect trade-only keys', 'invalid'),
    });
    await click(onButton());
    await typeInto(inputByLabel(dialog(), 'Live capital (USDT)'), '25000');
    await click(buttonByText(dialog(), 'Turn on live trading'));
    expect(dialog().textContent).toContain("Live trading wasn't turned on.");
    expect(dialog().textContent).toContain('The stored Binance keys can withdraw; connect trade-only keys');
    await click(buttonByText(dialog(), 'Cancel'));
    expect(text()).toContain('Turn on live trading…');
  });
});
