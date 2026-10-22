import { act } from 'react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { breaker, liveStatus, platformConfig, session } from '@/test/fixtures';
import {
  buttonByText,
  click,
  dialogPolyfill,
  flush,
  inputByLabel,
  mount,
  routeFetch,
  typeInto,
  unavailable,
  type Mounted,
  type Mutations,
  type Routes,
} from '@/test/pageHarness';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { default: RiskPage } = await import('./page');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');

const CONFIG = {
  scope: 'global',
  max_position_pct: 0.1,
  max_risk_per_trade: 0.005,
  max_portfolio_heat: 0.125,
  max_daily_loss_pct: 0.03,
  max_drawdown_pct: 0.15,
  max_concurrent_positions: 3,
  max_consecutive_losses: 5,
  kill_switch_active: false,
};

const BREAKERS = [
  breaker({ tier: 1, label: 'Reduce size 50%', threshold_pct: 3, status: 'Tripped', current_value: 10.5, active_since: '2026-10-20T09:00:00Z' }),
  breaker({ tier: 2, label: 'Halt new entries', threshold_pct: 5, status: 'Tripped', current_value: 10.5, active_since: '2026-10-20T09:00:00Z' }),
  breaker({ tier: 3, label: 'Flatten all sessions', threshold_pct: 10, status: 'Tripped', current_value: 10.5, active_since: '2026-10-20T09:00:00Z', requires_manual_review: true }),
  breaker({ tier: 4, label: 'Emergency shutdown', threshold_pct: 15, status: 'Warning', current_value: 10.5 }),
];

const STATUS = {
  current_drawdown: 10.5,
  max_drawdown_limit: 15,
  daily_loss: 120,
  daily_loss_limit: 300,
  equity: 9512.4,
  peak_equity: 10630.1,
  day_start_equity: 9632.4,
  consecutive_losses: 2,
  max_consecutive_losses: 5,
  circuit_breakers: BREAKERS,
  circuit_breakers_state: 'live',
  restrictions: {
    active_tier: 3,
    can_open_new_trades: false,
    size_multiplier: 0.5,
    must_flatten: true,
    is_shutdown: false,
    daily_halt_until: '2026-10-22T00:00:00Z',
    consecutive_pause_until: null,
  },
};

const KS_ON_EVENT = { id: 7, ts: '2026-10-21T12:00:00Z', scope: 'global', kind: 'kill_switch_on', tier: null, actor: 'user:soma', reason: 'kill switch (api)', details: { persisted: true } };

let ksState: 'active' | 'inactive' | 'unknown';

function routes(over: Routes = {}): Routes {
  return {
    '/api/risk/live-status': () => liveStatus({ kill_switch_state: ksState, kill_switch_active: ksState === 'unknown' ? null : ksState === 'active' }),
    '/api/trading/sessions': [],
    '/api/system/config': platformConfig(),
    '/api/risk/status?source=paper': STATUS,
    '/api/risk/events?limit=500': [KS_ON_EVENT],
    '/api/risk/config': CONFIG,
    '/api/risk/var?source=paper': { var_95: 210.5, var_99: 380, cvar_95: 260.25, portfolio_value: 9512.4, calculation_method: 'historical_simulation', sample_days: 12 },
    ...over,
  };
}

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

async function open(over: Routes = {}, mutations: Mutations = {}) {
  fetchApi.mockImplementation(routeFetch(routes(over), calls, mutations));
  page = await mount(
    <ShellStatusProvider>
      <RiskPage />
    </ShellStatusProvider>,
  );
  return page.container;
}

const dlg = (c: HTMLElement, title: string): HTMLElement => {
  const d = Array.from(c.querySelectorAll('dialog')).find((x) => x.querySelector('h2')?.textContent === title);
  if (!d) throw new Error(`no dialog ${title}`);
  return d;
};

const sent = (method: string, path: string) =>
  calls.filter((c) => c.path === path && c.init?.method === method).map((c) => JSON.parse(String(c.init?.body)));

beforeAll(dialogPolyfill);

beforeEach(() => {
  calls = [];
  ksState = 'inactive';
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
  vi.useRealTimers();
});

describe('kill switch section', () => {
  it('shows Stop all and no Release when the switch is off', async () => {
    const c = await open();
    expect(c.querySelector('#kill-switch')?.textContent).toContain('Off');
    expect(c.querySelector('button[aria-label="Stop all trading"]')).not.toBeNull();
    expect(() => buttonByText(c, 'Release…')).toThrow();
  });

  it('hides Release when the state is unknown', async () => {
    ksState = 'unknown';
    const c = await open();
    expect(c.querySelector('#kill-switch')?.textContent).toContain('State unknown');
    expect(() => buttonByText(c, 'Release…')).toThrow();
    expect(c.querySelector('button[aria-label="Stop all trading"]')).not.toBeNull();
  });

  it('shows the on-state, who turned it on, and stopped holders', async () => {
    ksState = 'active';
    const c = await open({
      '/api/trading/sessions': [session({ session_id: 'h1', strategy_name: 'Momentum 4h', status: 'stopped', flatten_incomplete: true, positions_at_stop: 1 })],
    });
    const text = c.querySelector('#kill-switch')?.textContent ?? '';
    expect(text).toContain('On. New orders are blocked.');
    expect(text).toContain('by you · via Hydra');
    expect(text).toContain('Still held in 1 stopped session: Momentum 4h');
    expect(buttonByText(c, 'Release…')).toBeTruthy();
  });

  it('says so when the on-event is not in the recent events', async () => {
    ksState = 'active';
    const c = await open({ '/api/risk/events?limit=500': [] });
    expect(c.querySelector('#kill-switch')?.textContent).toContain('On since: not in recent events');
  });

  it('releases only after RELEASE is typed exactly and a reason is given, then sends both', async () => {
    ksState = 'active';
    const c = await open();
    await click(buttonByText(c, 'Release…'));
    const d = dlg(c, 'Release the kill switch?');
    const primary = buttonByText(d, 'Release kill switch');
    expect(primary.disabled).toBe(true);

    await typeInto(inputByLabel(d, 'Type RELEASE to confirm'), 'release');
    await typeInto(inputByLabel(c, 'Reason'), 'market calm');
    expect(primary.disabled).toBe(true);

    await typeInto(inputByLabel(d, 'Type RELEASE to confirm'), 'RELEASE');
    await typeInto(inputByLabel(c, 'Reason'), '  ab ');
    expect(primary.disabled).toBe(true);

    await typeInto(inputByLabel(c, 'Reason'), '  market calm  ');
    expect(primary.disabled).toBe(false);
    await click(primary);

    expect(sent('DELETE', '/api/trading/kill-switch')).toEqual([{ confirm: 'RELEASE', reason: 'market calm' }]);
    expect(c.querySelector('[role="status"][aria-label="Outcome"]')?.textContent).toContain('Kill switch released at');
  });

  it('keeps the typed values and says nothing was released on a 503', async () => {
    ksState = 'active';
    const failed = new ApiError(503, JSON.stringify({ detail: { code: 'kill_switch_release_failed', message: 'x', state: 'unavailable' } }));
    const c = await open({}, { 'DELETE /api/trading/kill-switch': failed });
    await click(buttonByText(c, 'Release…'));
    const d = dlg(c, 'Release the kill switch?');
    await typeInto(inputByLabel(d, 'Type RELEASE to confirm'), 'RELEASE');
    await typeInto(inputByLabel(d, 'Reason'), 'market calm');
    await click(buttonByText(d, 'Release kill switch'));

    expect(inputByLabel(d, 'Type RELEASE to confirm').value).toBe('RELEASE');
    expect(inputByLabel(d, 'Reason').value).toBe('market calm');
    expect(d.textContent).toContain('the kill switch stays on');
    expect(buttonByText(d, 'Release kill switch').disabled).toBe(false);
  });
});

describe('kill switch section, stale and closed-dialog cases', () => {
  it('shows a failed release in the page panel when the dialog was closed mid-request', async () => {
    ksState = 'active';
    let reject: (e: Error) => void = () => {};
    const pending = new Promise<never>((_, rej) => (reject = rej));
    const failed = new ApiError(503, JSON.stringify({ detail: { code: 'kill_switch_release_failed', message: 'x', state: 'unavailable' } }));
    const c = await open({}, { 'DELETE /api/trading/kill-switch': () => pending });
    await click(buttonByText(c, 'Release…'));
    const d = dlg(c, 'Release the kill switch?');
    await typeInto(inputByLabel(d, 'Type RELEASE to confirm'), 'RELEASE');
    await typeInto(inputByLabel(d, 'Reason'), 'market calm');
    await click(buttonByText(d, 'Release kill switch'));
    await click(buttonByText(d, 'Close'));
    expect(c.querySelector('[aria-label="Outcome alert"]')?.textContent).toBe('');

    await act(async () => reject(failed));
    await flush();
    expect(c.querySelector('[aria-label="Outcome alert"]')?.textContent).toContain('the kill switch stays on');
  });

  it('does not show an older engagement as the current one after a later release', async () => {
    ksState = 'active';
    const off = { ...KS_ON_EVENT, id: 9, ts: '2026-10-21T13:00:00Z', kind: 'kill_switch_off' };
    const c = await open({ '/api/risk/events?limit=500': [off, KS_ON_EVENT] });
    expect(c.querySelector('#kill-switch')?.textContent).toContain('On since: not in recent events');
  });
});

describe('breakers', () => {
  it('lists four tiers and offers a reset on the active tier only', async () => {
    vi.useFakeTimers({ now: new Date('2026-10-21T14:00:00Z') });
    const c = await open();
    const text = c.querySelector('#breakers')?.textContent ?? '';
    expect(text).toContain('Tier 1: Reduce size 50%');
    expect(text).toContain('Tripped (included in tier 3)');
    expect(text).toContain('Drawdown 10.50% of 15.00% limit');
    expect(text).toContain('Daily loss halt until 22 Oct 00:00 UTC');
    expect(Array.from(c.querySelectorAll('#breakers button')).map((b) => b.textContent)).toEqual(['Reset tier 3…']);
  });

  it('says tiers 1 and 2 reset themselves', async () => {
    const c = await open({
      '/api/risk/status?source=paper': { ...STATUS, restrictions: { ...STATUS.restrictions, active_tier: 2 } },
    });
    expect(c.querySelector('#breakers')?.textContent).toContain('Resets automatically after 48 h once drawdown is below 5.00%.');
  });

  it('resets the active tier with the typed tier number and a reason', async () => {
    const c = await open({}, { 'POST /api/risk/circuit-breakers/3/reset': BREAKERS.map((b) => ({ ...b, current_value: 0, status: 'Normal' })) });
    await click(buttonByText(c, 'Reset tier 3…'));
    const d = dlg(c, 'Reset breaker tier 3?');
    expect(d.textContent).toContain('current equity (9,512.40 USDT)');
    await typeInto(inputByLabel(d, 'Type 3 to confirm'), '3');
    await typeInto(inputByLabel(d, 'Reason'), 'reviewed the loss');
    await click(buttonByText(d, 'Reset tier 3'));

    expect(sent('POST', '/api/risk/circuit-breakers/3/reset')).toEqual([{ reason: 'reviewed the loss', confirm_tier: 3 }]);
    expect(c.querySelector('[role="status"][aria-label="Outcome"]')?.textContent).toContain('Breaker tier 3 reset at');
  });

  it('closes and explains when the tier is no longer active', async () => {
    const stale = new ApiError(409, JSON.stringify({ detail: { code: 'tier_not_active', message: 'x', state: 'invalid', active_tier: 2 } }));
    const c = await open({}, { 'POST /api/risk/circuit-breakers/3/reset': stale });
    await click(buttonByText(c, 'Reset tier 3…'));
    const d = dlg(c, 'Reset breaker tier 3?');
    await typeInto(inputByLabel(d, 'Type 3 to confirm'), '3');
    await typeInto(inputByLabel(d, 'Reason'), 'reviewed the loss');
    await click(buttonByText(d, 'Reset tier 3'));

    expect(c.querySelector('[role="status"][aria-label="Outcome"]')?.textContent).toContain("Tier 3 isn't active any more");
    expect(d.hasAttribute('open')).toBe(false);
  });
});

describe('paper ledger', () => {
  it('shows the ledger values exactly', async () => {
    const c = await open();
    const text = c.querySelector('#ledger')?.textContent ?? '';
    expect(text).toContain('9,512.40 USDT');
    expect(text).toContain('10,630.10 USDT');
    expect(text).toContain('9,632.40 USDT');
  });

  it('resets with RESET and a reason', async () => {
    const c = await open({}, { 'POST /api/risk/paper-account/reset': { ...STATUS, current_drawdown: 0 } });
    await click(buttonByText(c, 'Reset paper ledger…'));
    const d = dlg(c, 'Reset the paper ledger?');
    await typeInto(inputByLabel(d, 'Type RESET to confirm'), 'RESET');
    await typeInto(inputByLabel(d, 'Reason'), 'fresh start');
    await click(buttonByText(d, 'Reset paper ledger'));

    expect(sent('POST', '/api/risk/paper-account/reset')).toEqual([{ confirm: 'RESET', reason: 'fresh start' }]);
    expect(c.querySelector('[role="status"][aria-label="Outcome"]')?.textContent).toContain('Drawdown 0.00%.');
  });

  it('keeps the typed values when the reset is unavailable', async () => {
    const failed = new ApiError(503, JSON.stringify({ detail: { code: 'paper_reset_unavailable', message: 'x', state: 'unavailable' } }));
    const c = await open({}, { 'POST /api/risk/paper-account/reset': failed });
    await click(buttonByText(c, 'Reset paper ledger…'));
    const d = dlg(c, 'Reset the paper ledger?');
    await typeInto(inputByLabel(d, 'Type RESET to confirm'), 'RESET');
    await typeInto(inputByLabel(d, 'Reason'), 'fresh start');
    await click(buttonByText(d, 'Reset paper ledger'));

    expect(inputByLabel(d, 'Reason').value).toBe('fresh start');
    expect(d.textContent).toContain('Not reset.');
  });
});

describe('limits', () => {
  const edit = async (c: HTMLElement, label: string, value: string) => {
    await click(buttonByText(c, 'Edit limits'));
    await typeInto(inputByLabel(c, label), value);
  };

  it('shows all seven limits exactly', async () => {
    const c = await open();
    const text = c.querySelector('#limits')?.textContent ?? '';
    for (const t of ['0.5%', '12.5%', '10%', '3%', '15%', 'Max concurrent positions3', 'Max consecutive losses5']) expect(text).toContain(t);
  });

  it('saves a tightened limit directly, sending only that field', async () => {
    const c = await open();
    await edit(c, 'Max drawdown (%)', '10');
    await click(buttonByText(c, 'Save limits'));

    expect(sent('PUT', '/api/risk/config')).toEqual([{ max_drawdown_pct: 0.1 }]);
    expect(c.querySelector('[role="status"][aria-label="Outcome"]')?.textContent).toContain('Limits saved.');
  });

  it('asks with a diff before loosening and saves nothing until confirmed', async () => {
    const c = await open();
    await edit(c, 'Max drawdown (%)', '25');
    await typeInto(inputByLabel(c, 'Max daily loss (%)'), '2');
    await click(buttonByText(c, 'Save limits'));

    expect(sent('PUT', '/api/risk/config')).toEqual([]);
    const dialog = dlg(c, 'Loosen 1 risk limit?').textContent ?? '';
    expect(dialog).toContain('Max drawdown 15% → 25% (looser)');
    expect(dialog).toContain('Max daily loss 3% → 2%');
    expect(dialog).not.toContain('2% (looser)');

    await click(buttonByText(c, 'Save looser limits'));
    expect(sent('PUT', '/api/risk/config')).toEqual([{ max_drawdown_pct: 0.25, max_daily_loss_pct: 0.02 }]);
  });

  it('does not save when the dialog is declined', async () => {
    const c = await open();
    await edit(c, 'Max drawdown (%)', '25');
    await click(buttonByText(c, 'Save limits'));
    await click(buttonByText(c, 'Keep current'));
    expect(sent('PUT', '/api/risk/config')).toEqual([]);
  });

  it('keeps a non-dismissible not-in-force notice in the Limits card until a save succeeds', async () => {
    const failed = new ApiError(503, JSON.stringify({ detail: { code: 'risk_reload_failed', message: 'x', state: 'unavailable' } }));
    const c = await open({}, { 'PUT /api/risk/config': (n: number) => (n === 1 ? failed : {}) });
    await edit(c, 'Max drawdown (%)', '10');
    await click(buttonByText(c, 'Save limits'));

    const notice = () => c.querySelector('#limits [role="alert"][aria-label="Limits not in force"]');
    expect(notice()?.textContent).toContain('Saved, but not in force.');
    expect(c.querySelector('[aria-label="Outcome alert"]')?.textContent).toBe('');
    expect(notice()?.querySelector('[aria-label="Dismiss"]')).toBeNull();

    await click(buttonByText(c, 'Try again'));
    expect(sent('PUT', '/api/risk/config')).toEqual([{ max_drawdown_pct: 0.1 }, { max_drawdown_pct: 0.1 }]);
    expect(notice()).toBeNull();
    expect(c.querySelector('[role="status"][aria-label="Outcome"]')?.textContent).toContain('Limits saved.');
  });

  it('keeps the notice when an unrelated outcome replaces the page panel', async () => {
    ksState = 'active';
    const failed = new ApiError(503, JSON.stringify({ detail: { code: 'risk_reload_failed', message: 'x', state: 'unavailable' } }));
    const c = await open({}, { 'PUT /api/risk/config': failed });
    await edit(c, 'Max drawdown (%)', '10');
    await click(buttonByText(c, 'Save limits'));
    await click(buttonByText(c, 'Release…'));
    const d = dlg(c, 'Release the kill switch?');
    await typeInto(inputByLabel(d, 'Type RELEASE to confirm'), 'RELEASE');
    await typeInto(inputByLabel(d, 'Reason'), 'market calm');
    await click(buttonByText(d, 'Release kill switch'));

    expect(c.querySelector('[role="status"][aria-label="Outcome"]')?.textContent).toContain('Kill switch released at');
    expect(c.querySelector('#limits [aria-label="Limits not in force"]')).not.toBeNull();
  });
});

describe('value at risk', () => {
  it('shows NO DATA, not zeros, below five days of history', async () => {
    const c = await open({ '/api/risk/var?source=paper': { var_95: 0, var_99: 0, cvar_95: 0, portfolio_value: 9512.4, calculation_method: 'historical_simulation', sample_days: 3 } });
    const text = c.textContent ?? '';
    expect(text).toContain('NO DATA');
    expect(text).toContain('Needs 5 days of paper P&L; have 3.');
    expect(text).not.toContain('VaR 95%');
  });

  it('shows the three measures from five days on', async () => {
    const c = await open();
    const text = c.textContent ?? '';
    expect(text).toContain('VaR 95%210.50 USDT');
    expect(text).toContain('VaR 99%380.00 USDT');
    expect(text).toContain('CVaR 95%260.25 USDT');
  });

  it('shows UNAVAILABLE when equity is unknown', async () => {
    const c = await open({ '/api/risk/var?source=paper': unavailable('No account equity has been marked yet') });
    expect(c.textContent).toContain('UNAVAILABLE');
    expect(c.textContent).toContain('No account equity has been marked yet');
  });
});

describe('activity log', () => {
  it('writes events as sentences with the raw kind and details one click away', async () => {
    const c = await open({
      '/api/risk/events?limit=500': [
        KS_ON_EVENT,
        { id: 6, ts: '2026-10-21T11:00:00Z', scope: 'global', kind: 'cb_escalate', tier: 3, actor: 'risk_engine', reason: 'drawdown', details: { text: '10.5% of 10%' } },
      ],
    });
    const log = c.querySelector('#activity')?.textContent ?? '';
    expect(log).toContain('Kill switch turned on');
    expect(log).toContain('Breaker moved to tier 3: Flatten all sessions');
    expect(log).toContain('by engine (automatic)');
    expect(log).toContain('cb_escalate');
    expect(log).toContain('{"text":"10.5% of 10%"}');
  });

  it('shows the empty copy', async () => {
    const c = await open({ '/api/risk/events?limit=500': [] });
    expect(c.querySelector('#activity')?.textContent).toContain('No risk events yet.');
  });
});
