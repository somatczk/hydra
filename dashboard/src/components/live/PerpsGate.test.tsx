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
import { liveChallenge, liveSession, perpSession, perpsDisarmResponse, perpsStatus } from '@/test/fixtures';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('next/navigation', () => ({ usePathname: () => '/settings' }));

const { PerpsGate } = await import('./PerpsGate');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');

const APPROVE_PATH = '/api/activity/approvals/3f0b9e2a-2000-4000-8000-000000000001/approve';
const PERPS_FACTS =
  "Live perp sessions place real leveraged orders on Binance USD⌂-M futures with real money — up to 3× on isolated margin, so each position's loss is capped at the margin it posted and a liquidation takes that margin to zero. Paper sessions keep running. Every live perp session start and manual live order needs its own one-time code; once a live perp session starts, its strategy trades under that start's authorization — bounded by the kill switch, risk limits and the exchange's reduce-only stop ladder.";

/** The shared envelope from `api_error`: extras sit beside code/message/state. */
const envelope = (status: number, code: string, message: string, state: string, extra: Record<string, unknown> = {}) =>
  new ApiError(status, JSON.stringify({ detail: { code, message, state, ...extra } }));

let m: Mounted;
let calls: Array<{ path: string; init?: RequestInit }>;
let armed: boolean;
let list: Array<ReturnType<typeof perpSession>>;

/** One mock per test: the perps-status route reads `armed` at call time so a leg can flip it. */
function begin(routes: Routes = {}, mutations: Mutations = {}) {
  fetchApi.mockImplementation(
    routeFetch(
      {
        '/api/live/perps/status': () => perpsStatus({ perps_armed: armed, perps_capital: armed ? 500 : null }),
        '/api/trading/sessions': () => list,
        ...routes,
      },
      calls,
      { 'POST /api/live/perps/arm': liveChallenge({ params: { perps_capital: 500 } }), ...mutations },
    ),
  );
  return mount(<ShellStatusProvider><PerpsGate /></ShellStatusProvider>);
}

const gate = begin;
const text = () => m.container.textContent ?? '';
/** The mounted section only — the two closed ceremony dialogs also sit in the DOM. */
const sectionNode = () =>
  Array.from(m.container.querySelectorAll('h2')).find((h) => h.textContent === 'Perps trading (USD⌂-M)')?.closest('section') ?? m.container;
const section = () => sectionNode().textContent ?? '';
const dialog = () => m.container.querySelector('dialog[open]') as HTMLDialogElement;
const onButton = () => buttonByText(m.container, 'Turn on perps trading…');
const offButton = () => buttonByText(m.container, 'Turn off perps trading…');

/** Opens the arm ceremony and drives it to the code leg. */
async function driveToCode(mutations: Mutations = {}) {
  m = await gate({}, mutations);
  await click(onButton());
  await typeInto(inputByLabel(dialog(), 'Perps capital (USDT)'), '500');
  await click(buttonByText(dialog(), 'Turn on perps trading'));
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

describe('PerpsGate', () => {
  it('shows the off state with the arm door, never claiming the spot gate arms perps', async () => {
    m = await gate();
    expect(section()).toContain('Off. Perp strategies keep running paper until this leveraged gate is armed — arming live trading never arms perps.');
    expect(onButton()).toBeTruthy();
    expect(section()).not.toContain('Perps trading is on.');
    expect(section()).not.toMatch(/\bLIVE\b/);
  });

  it('arms through the L4 ceremony: capital → the engine code shown in-flow → typed back → on', async () => {
    m = await gate({}, {
      [`POST ${APPROVE_PATH}`]: () => {
        armed = true;
        return { executed: true, result: { status: 'armed', perps_armed: true }, actor: 'user:soma', approved_by: 'user:soma' };
      },
    });
    await click(onButton());
    const d = dialog();
    expect(d.textContent).toContain(PERPS_FACTS);
    const primary = buttonByText(d, 'Turn on perps trading');
    expect(primary.disabled).toBe(true);
    await typeInto(inputByLabel(d, 'Perps capital (USDT)'), '500');
    await click(primary);
    const armCall = calls.find((c) => c.path === '/api/live/perps/arm');
    expect(JSON.parse(armCall?.init?.body as string)).toEqual({
      perps_capital: 500,
      reason: 'Perps trading armed from Settings',
    });
    expect(d.textContent).toContain('482913');
    const codeInput = inputByLabel(d, 'One-time code');
    expect(codeInput.autocomplete).toBe('one-time-code');
    expect(codeInput.inputMode).toBe('numeric');
    expect(buttonByText(d, 'Turn on perps trading').disabled).toBe(true);
    await typeInto(codeInput, '482913');
    await click(buttonByText(d, 'Turn on perps trading'));
    expect(calls.filter((c) => c.path === APPROVE_PATH).map((c) => JSON.parse(c.init?.body as string))).toEqual([
      { code: '482913' },
    ]);
    expect(text()).toContain('Perps trading is on');
    expect(text()).toContain('500.00 leveraged capital');
    expect(text()).toContain('Each live perp session start and manual live order still needs its own one-time code.');
    expect(offButton()).toBeTruthy();
    expect(m.container.querySelector('dialog[open]')).toBeNull();
  });

  it('says the honest no-op when the gate was already armed elsewhere', async () => {
    m = await gate({}, {
      [`POST ${APPROVE_PATH}`]: {
        executed: true,
        result: { status: 'already_armed', perps_armed: true, message: 'Perps trading is already armed; nothing changed' },
        actor: 'user:soma',
        approved_by: 'user:soma',
      },
    });
    await click(onButton());
    await typeInto(inputByLabel(dialog(), 'Perps capital (USDT)'), '500');
    await click(buttonByText(dialog(), 'Turn on perps trading'));
    await typeInto(inputByLabel(dialog(), 'One-time code'), '482913');
    await click(buttonByText(dialog(), 'Turn on perps trading'));
    expect(dialog().textContent).toContain('Perps trading is already armed; nothing changed.');
    expect(text()).not.toContain('Perps trading is on');
  });

  it('keeps a wrong code inline with focus back on the field, and the code kept', async () => {
    await driveToCode({ [`POST ${APPROVE_PATH}`]: envelope(422, 'code_mismatch', "That code didn't match. Nothing changed.", 'invalid', { attempts_left: 4 }) });
    const d = dialog();
    await typeInto(inputByLabel(d, 'One-time code'), '482913');
    await click(buttonByText(d, 'Turn on perps trading'));
    expect(d.textContent).toContain("That code didn't match. Nothing changed. 4 attempts left.");
    expect(inputByLabel(d, 'One-time code').value).toBe('482913');
    expect(document.activeElement).toBe(inputByLabel(d, 'One-time code'));
    expect(text()).not.toContain('Perps trading is on');
  });

  it('names the lockout honestly at the last wrong attempt', async () => {
    await driveToCode({ [`POST ${APPROVE_PATH}`]: envelope(422, 'code_mismatch', "That code didn't match. Nothing changed.", 'invalid', { attempts_left: 0 }) });
    const d = dialog();
    await typeInto(inputByLabel(d, 'One-time code'), '482913');
    await click(buttonByText(d, 'Turn on perps trading'));
    expect(d.textContent).toContain('No attempts left — start again for a fresh code.');
  });

  it('surfaces the fail-closed futures-permission refusal verbatim: the key cannot trade futures', async () => {
    m = await gate({}, {
      [`POST ${APPROVE_PATH}`]: envelope(
        409,
        'credentials_not_futures_capable',
        'The stored binance key cannot trade futures; perps trading needs futures enabled on the key (enable futures trading on the key and reconnect it in Settings)',
        'invalid',
      ),
    });
    await click(onButton());
    await typeInto(inputByLabel(dialog(), 'Perps capital (USDT)'), '500');
    await click(buttonByText(dialog(), 'Turn on perps trading'));
    await typeInto(inputByLabel(dialog(), 'One-time code'), '482913');
    await click(buttonByText(dialog(), 'Turn on perps trading'));
    expect(dialog().textContent).toContain('Nothing was armed.');
    expect(dialog().textContent).toContain('cannot trade futures');
    expect(text()).not.toContain('Perps trading is on');
  });

  it('surfaces the withdrawals-on refusal and the missing-keys refusal the same way', async () => {
    m = await gate({}, {
      [`POST ${APPROVE_PATH}`]: envelope(
        409,
        'credentials_not_futures_capable',
        'The stored binance key has withdrawals enabled; perps trading needs a trade-only key (disable withdrawals on the key and reconnect it in Settings)',
        'invalid',
      ),
    });
    await click(onButton());
    await typeInto(inputByLabel(dialog(), 'Perps capital (USDT)'), '500');
    await click(buttonByText(dialog(), 'Turn on perps trading'));
    await typeInto(inputByLabel(dialog(), 'One-time code'), '482913');
    await click(buttonByText(dialog(), 'Turn on perps trading'));
    expect(dialog().textContent).toContain('Nothing was armed.');
    expect(dialog().textContent).toContain('withdrawals enabled');
  });

  it('points at Risk when the kill switch refuses the arm', async () => {
    m = await gate({}, {
      [`POST ${APPROVE_PATH}`]: envelope(409, 'kill_switch_active', 'The kill switch is active; release it before arming perps trading', 'invalid'),
    });
    await click(onButton());
    await typeInto(inputByLabel(dialog(), 'Perps capital (USDT)'), '500');
    await click(buttonByText(dialog(), 'Turn on perps trading'));
    await typeInto(inputByLabel(dialog(), 'One-time code'), '482913');
    await click(buttonByText(dialog(), 'Turn on perps trading'));
    expect(dialog().textContent).toContain('Nothing was armed.');
    expect(dialog().textContent).toContain('The kill switch is active');
    expect(dialog().querySelector('a[href="/risk#kill-switch"]')?.textContent).toBe('Release on Risk');
  });

  it('states an expired code and requires a fresh challenge', async () => {
    await driveToCode({ [`POST ${APPROVE_PATH}`]: envelope(409, 'approval_expired', 'This approval has expired', 'invalid') });
    const d = dialog();
    await typeInto(inputByLabel(d, 'One-time code'), '482913');
    await click(buttonByText(d, 'Turn on perps trading'));
    expect(d.textContent).toMatch(/The one-time code expired at .* UTC\. Nothing changed\./);
    expect(d.textContent).toContain('Arm it again for a fresh code.');
    expect(inputByLabel(d, 'Perps capital (USDT)')).toBeTruthy();
    await click(buttonByText(d, 'Turn on perps trading'));
    expect(calls.filter((c) => c.path === '/api/live/perps/arm')).toHaveLength(2);
  });

  it('names the still-running live perp sessions when turning off, and keeps them visible after (R10)', async () => {
    armed = true;
    list = [
      perpSession({ session_id: 'perp-1', strategy_name: 'Perp momentum', trading_mode: 'live' }),
      perpSession({ session_id: 'perp-2', strategy_name: 'Perp revert', trading_mode: 'live' }),
    ];
    m = await gate({}, {
      'POST /api/live/perps/disarm': () => {
        armed = false;
        return perpsDisarmResponse({ running_live_perps_sessions: ['perp-1', 'perp-2'], running_live_perps_sessions_count: 2 });
      },
    });
    expect(text()).toContain('Perps trading is on');
    await click(offButton());
    const d = dialog();
    expect(d.textContent).toContain("2 live perp sessions keep running under their start's authorization");
    expect(d.textContent).toContain('Perp momentum');
    expect(d.textContent).toContain('Perp revert');
    expect(d.querySelectorAll('a[href="/trading/perp-1"], a[href="/trading/perp-2"]')).toHaveLength(4);
    await click(buttonByText(d, 'Turn off perps trading'));
    expect(calls.filter((c) => c.path === '/api/live/perps/disarm').map((c) => JSON.parse(c.init?.body as string))).toEqual([
      { reason: 'Perps trading turned off from Settings' },
    ]);
    expect(section()).toContain('Off. Perp strategies keep running paper until this leveraged gate is armed');
    expect(section()).toContain('Perp momentum');
    expect(section()).toContain('Perp revert');
    expect(sectionNode().querySelectorAll('a[href="/trading/perp-1"]')).toHaveLength(2);
  });

  it('never says no live perp session over an unreadable list, and still names the sessions R10 confirmed', async () => {
    armed = true;
    m = await gate(
      { '/api/trading/sessions': unavailable('sessions unreadable') },
      {
        'POST /api/live/perps/disarm': () => {
          armed = false;
          return perpsDisarmResponse({ running_live_perps_sessions: ['perp-1'], running_live_perps_sessions_count: 1 });
        },
      },
    );
    await click(offButton());
    expect(dialog().textContent).toContain('The sessions list is unreadable — live perp sessions may be running. Check /trading before disarming.');
    expect(dialog().textContent).not.toContain('No live perp session is running.');
    await click(buttonByText(dialog(), 'Turn off perps trading'));
    expect(text()).toContain('1 live perp session keeps running');
    expect(text()).toContain('perp-1');
  });

  it('never hides a running live perp session in the off state, with its Stop link', async () => {
    list = [perpSession({ strategy_name: 'Perp momentum', trading_mode: 'live' })];
    m = await gate();
    expect(section()).toContain('Off. Perp strategies keep running paper until this leveraged gate is armed');
    expect(section()).toContain("1 live perp session keeps running under its start's authorization");
    expect(section()).toContain('Perp momentum');
    const links = Array.from(sectionNode().querySelectorAll('a[href="/trading/a1b2c3d4-0000-4000-8000-000000000001"]')).map(
      (a) => a.textContent,
    );
    expect(links).toEqual(['Perp momentum', 'Stop']);
  });

  it('names only perp sessions — a running live spot session is not the perps gate\'s to hide', async () => {
    list = [liveSession({ strategy_name: 'Spot live' })];
    m = await gate();
    expect(section()).not.toContain('Spot live');
  });

  it('hides every door while the perps status cannot be read', async () => {
    m = await gate({ '/api/live/perps/status': unavailable('perps state unavailable') });
    expect(text()).toContain('Perps gate state unknown.');
    expect(text()).not.toContain('Turn on perps trading…');
    expect(text()).not.toContain('Turn off perps trading…');
  });

  it('hides the on-button after the code service is down, with an honest recovery probe', async () => {
    m = await gate({}, { 'POST /api/live/perps/arm': unavailable('approval store unavailable') });
    await click(onButton());
    await typeInto(inputByLabel(dialog(), 'Perps capital (USDT)'), '500');
    await click(buttonByText(dialog(), 'Turn on perps trading'));
    expect(dialog().textContent).toContain("Perps trading wasn't turned on.");
    expect(dialog().textContent).toContain('Live actions are hidden until the code service recovers.');
    await click(buttonByText(dialog(), 'Cancel'));
    expect(section()).toContain("Perps trading wasn't turned on.");
    expect(section()).toContain('Perps actions are hidden until the code service recovers.');
    expect(text()).not.toContain('Turn on perps trading…');
    await click(buttonByText(m.container, 'Try again'));
    expect(text()).toContain('Turn on perps trading…');
  });

  it('stays not-confirmed when the approve times out — never claimed off (R30)', async () => {
    m = await gate({}, {
      [`POST ${APPROVE_PATH}`]: envelope(
        503,
        'engine_unreachable',
        'The trading engine did not answer within 20s — the operation may still be executing; check the session or order state before re-submitting',
        'unavailable',
      ),
    });
    await click(onButton());
    await typeInto(inputByLabel(dialog(), 'Perps capital (USDT)'), '500');
    await click(buttonByText(dialog(), 'Turn on perps trading'));
    await typeInto(inputByLabel(dialog(), 'One-time code'), '482913');
    await click(buttonByText(dialog(), 'Turn on perps trading'));
    expect(dialog().textContent).toContain('Not confirmed.');
    expect(dialog().textContent).toContain("We couldn't reach the engine, so we don't know whether perps trading was turned on.");
    expect(dialog().textContent).toContain('the operation may still be executing');
  });
});
