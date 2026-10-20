import { act } from 'react';
import { ApiError } from '@/lib/api';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buttonByText,
  click,
  inputByLabel,
  mount,
  routeFetch,
  sectionText,
  typeInto,
  unavailable,
  type Mounted,
  type Routes,
} from '@/test/pageHarness';

const fetchApi = vi.fn();
const toast = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast }) }));

const { default: RiskPage } = await import('./page');

const LIVE_BREAKERS = [
  { tier: 1, label: 'Reduce size 50%', threshold: '3.00% drawdown', threshold_pct: 3, current_value: 10.5, status: 'Tripped', active_since: '2026-10-20T09:00:00Z', requires_manual_review: false },
  { tier: 2, label: 'Halt new entries', threshold: '5.00% drawdown', threshold_pct: 5, current_value: 10.5, status: 'Tripped', active_since: '2026-10-20T09:00:00Z', requires_manual_review: false },
  { tier: 3, label: 'Flatten all sessions', threshold: '10.00% drawdown', threshold_pct: 10, current_value: 10.5, status: 'Tripped', active_since: '2026-10-20T09:00:00Z', requires_manual_review: true },
  { tier: 4, label: 'Emergency shutdown', threshold: '15.00% drawdown', threshold_pct: 15, current_value: 10.5, status: 'Warning', active_since: null, requires_manual_review: false },
];

const RISK_CONFIG = {
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

function baseRoutes(): Routes {
  return {
    '/api/system/config': { trading_mode: 'paper' },
    '/api/risk/status?source=paper': {
      current_drawdown: 2.5,
      max_drawdown_limit: 15,
      daily_loss: 120,
      daily_loss_limit: 300,
      equity: 9880,
      peak_equity: 10000,
      day_start_equity: 10000,
      consecutive_losses: 0,
      max_consecutive_losses: 5,
      circuit_breakers: null,
      circuit_breakers_state: 'unavailable',
      restrictions: null,
    },
    '/api/risk/events?limit=20': [],
    '/api/risk/var?source=paper': unavailable(),
    '/api/portfolio/daily-pnl?source=paper': [{ date: '2026-10-19', pnl: -50 }],
    '/api/risk/config': RISK_CONFIG,
    '/api/risk/live-status': {
      kill_switch_active: false,
      kill_switch_state: 'inactive',
      running_sessions: 0,
      circuit_breaker_restrictions: null,
      circuit_breakers_state: 'unavailable',
    },
  };
}

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

beforeEach(() => {
  calls = [];
  fetchApi.mockReset();
  toast.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
  vi.useRealTimers();
});

function puts(): unknown[] {
  return calls
    .filter((c) => c.init?.method === 'PUT')
    .map((c) => JSON.parse(String(c.init?.body)));
}

describe('RiskPage daily loss tracker', () => {
  it('shows UNAVAILABLE on a daily-pnl 503 instead of "No data"', async () => {
    const routes = baseRoutes();
    routes['/api/portfolio/daily-pnl?source=paper'] = unavailable();
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<RiskPage />);

    const text = sectionText(page.container, 'Daily Loss Tracker');
    expect(text).toContain('UNAVAILABLE');
    expect(text).toContain('Database is not available');
    expect(text).not.toContain('No data');
  });

  it('keeps "No data" for a genuinely empty history', async () => {
    const routes = baseRoutes();
    routes['/api/portfolio/daily-pnl?source=paper'] = [];
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<RiskPage />);

    const text = sectionText(page.container, 'Daily Loss Tracker');
    expect(text).toContain('No data');
    expect(text).not.toContain('UNAVAILABLE');
  });
});

describe('RiskPage risk limits form', () => {
  it('loads sub-percent limits without integer rounding', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<RiskPage />);

    expect(inputByLabel(page.container, 'Max Risk Per Trade (%)').value).toBe('0.5');
    expect(inputByLabel(page.container, 'Max Portfolio Heat (%)').value).toBe('12.5');
    expect(inputByLabel(page.container, 'Max Position Size (%)').value).toBe('10');
  });

  it('saves only the edited field', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<RiskPage />);

    await typeInto(inputByLabel(page.container, 'Max Drawdown (%)'), '20');
    await click(buttonByText(page.container, 'Save Risk Limits'));

    expect(puts()).toEqual([{ max_drawdown_pct: 0.2 }]);
  });

  it('does not overwrite an in-progress edit on the 30s poll', async () => {
    vi.useFakeTimers();
    let cfg = RISK_CONFIG;
    const routes = baseRoutes();
    routes['/api/risk/config'] = () => cfg;
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<RiskPage />);

    const input = inputByLabel(page.container, 'Max Daily Loss (%)');
    await typeInto(input, '4');
    cfg = { ...RISK_CONFIG, max_daily_loss_pct: 0.05 };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(calls.filter((c) => c.path === '/api/risk/config').length).toBeGreaterThan(1);
    expect(input.value).toBe('4');
  });
});

describe('RiskPage live risk engine state', () => {
  it('renders live breakers, restrictions and the risk event trail', async () => {
    const routes = baseRoutes();
    routes['/api/risk/status?source=paper'] = {
      ...(routes['/api/risk/status?source=paper'] as object),
      current_drawdown: 10.5,
      consecutive_losses: 2,
      circuit_breakers: LIVE_BREAKERS,
      circuit_breakers_state: 'live',
      restrictions: {
        active_tier: 3,
        can_open_new_trades: false,
        size_multiplier: 0.5,
        must_flatten: true,
        is_shutdown: false,
        daily_halt_until: '2026-10-21T00:00:00Z',
        consecutive_pause_until: null,
      },
    };
    routes['/api/risk/events?limit=20'] = [
      { id: 1, ts: '2026-10-20T09:00:00Z', scope: 'paper', kind: 'cb_escalate', tier: 3, actor: 'risk_engine', reason: 'drawdown 0.1050 >= 0.10', details: {} },
    ];
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<RiskPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Flatten all sessions');
    expect(text).toContain('Manual review required');
    expect(text).not.toContain('risk service not running');
    const strip = page.container.querySelector('[data-testid="risk-restrictions"]')?.textContent ?? '';
    expect(strip).toContain('New entries blocked');
    expect(strip).toContain('x0.5');
    expect(strip).toContain('Losing streak 2 / 5');
    expect(strip).toContain('Daily halt until');
    const events = sectionText(page.container, 'Risk Events');
    expect(events).toContain('cb_escalate');
    expect(events).toContain('risk_engine');
  });

  it('does not guess a source when the trading mode cannot be read', async () => {
    const routes = baseRoutes();
    routes['/api/system/config'] = unavailable();
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<RiskPage />);

    expect(calls.some((c) => c.path.includes('source='))).toBe(false);
    expect(page.container.textContent).toContain('Database is not available');
  });
});

describe('RiskPage kill switch release', () => {
  it('needs RELEASE typed and a reason before it sends the release', async () => {
    const routes = baseRoutes();
    routes['/api/risk/config'] = { ...RISK_CONFIG, kill_switch_active: true };
    routes['/api/risk/live-status'] = {
      kill_switch_active: true,
      kill_switch_state: 'active',
      running_sessions: 0,
      circuit_breaker_restrictions: null,
      circuit_breakers_state: 'unavailable',
    };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('release');
    page = await mount(<RiskPage />);

    await click(buttonByText(page.container, 'Release Kill Switch'));
    expect(calls.filter((c) => c.init?.method === 'DELETE')).toEqual([]);

    prompt.mockReturnValueOnce('RELEASE').mockReturnValueOnce('  ');
    await click(buttonByText(page.container, 'Release Kill Switch'));
    expect(calls.filter((c) => c.init?.method === 'DELETE')).toEqual([]);

    prompt.mockReturnValueOnce('RELEASE').mockReturnValueOnce(' post-mortem done ');
    await click(buttonByText(page.container, 'Release Kill Switch'));
    const deletes = calls.filter((c) => c.init?.method === 'DELETE');
    expect(deletes.map((c) => [c.path, JSON.parse(String(c.init?.body))])).toEqual([
      ['/api/trading/kill-switch', { confirm: 'RELEASE', reason: 'post-mortem done' }],
    ]);
    prompt.mockRestore();
  });
});

describe('RiskPage kill switch activation', () => {
  const ACTIVATED = {
    active: true,
    message: 'Kill switch latched; 2 position(s) could NOT be closed - check the sessions',
    sessions_stopped: 2,
    positions_remaining: 2,
    persisted: true,
    flatten_failures: ['s1 BTCUSDT: no market price', 's2 ETHUSDT: ledger halted'],
  };

  function withActivation(routes: Routes, answer: () => unknown) {
    const get = routeFetch(routes, calls);
    return async (path: string, init?: RequestInit): Promise<unknown> => {
      if (path === '/api/trading/kill-switch' && init?.method === 'POST') {
        calls.push({ path, init });
        const value = answer();
        if (value instanceof Error) throw value;
        return value;
      }
      return get(path, init);
    };
  }

  function banner(container: HTMLElement): string {
    const btn = buttonByText(container, 'Release Kill Switch');
    return btn.closest('.rounded-xl')?.textContent ?? '';
  }

  async function activate(answer: () => unknown): Promise<Mounted> {
    fetchApi.mockImplementation(withActivation(baseRoutes(), answer));
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const mounted = await mount(<RiskPage />);
    expect(mounted.container.textContent).toContain('System Normal');
    await click(buttonByText(mounted.container, 'Kill Switch'));
    confirm.mockRestore();
    return mounted;
  }

  it('shows LATCHED with the positions it could not flatten', async () => {
    page = await activate(() => ACTIVATED);

    const text = banner(page.container);
    expect(text).toContain('LATCHED — 2 positions could not be flattened');
    expect(text).toContain('s1 BTCUSDT: no market price');
    expect(text).toContain('s2 ETHUSDT: ledger halted');
    expect(text).not.toContain('Not persisted');
    expect(page.container.textContent).toContain('Kill Switch Active');
    expect(page.container.textContent).not.toContain('System Normal');
    expect(toast).toHaveBeenCalledWith('error', 'Kill switch LATCHED — 2 positions could not be flattened');
    expect(toast).not.toHaveBeenCalledWith('warning', expect.anything());
  });

  it('says all positions closed when nothing remains', async () => {
    page = await activate(() => ({ ...ACTIVATED, positions_remaining: 0, flatten_failures: [] }));

    const text = banner(page.container);
    expect(text).toContain('Kill Switch Active');
    expect(text).not.toContain('LATCHED');
    expect(toast).toHaveBeenCalledWith('warning', 'Kill switch ACTIVATED — all positions closed and trading halted');
  });

  it('treats a 503 kill_switch_not_persisted as latched, not inactive', async () => {
    const detail = {
      code: 'kill_switch_not_persisted',
      message: 'Kill switch latched; 1 position(s) could NOT be closed - check the sessions; the latch is in memory only and is lost on restart',
      state: 'unavailable',
      active: true,
      sessions_stopped: 1,
      positions_remaining: 1,
      persisted: false,
      flatten_failures: ['s1 BTCUSDT: no market price'],
    };
    page = await activate(() => new ApiError(503, JSON.stringify({ detail })));

    const text = banner(page.container);
    expect(text).toContain('LATCHED — 1 position could not be flattened');
    expect(text).toContain('Not persisted — the latch is in memory only and is lost on restart.');
    expect(page.container.textContent).not.toContain('System Normal');
    expect(toast).toHaveBeenCalledWith('error', detail.message);
  });

  it('keeps the switch inactive on any other failure', async () => {
    page = await activate(() => unavailable('engine down'));

    expect(page.container.textContent).toContain('System Normal');
    expect(() => buttonByText(page!.container, 'Release Kill Switch')).toThrow();
    expect(toast).toHaveBeenCalledWith('error', 'engine down');
  });
});
