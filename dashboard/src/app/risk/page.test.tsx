import { act } from 'react';
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

const RISK_CONFIG = {
  scope: 'global',
  max_position_pct: 0.1,
  max_risk_per_trade: 0.005,
  max_portfolio_heat: 0.125,
  max_daily_loss_pct: 0.03,
  max_drawdown_pct: 0.15,
  max_concurrent_positions: 3,
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
      circuit_breakers: null,
      circuit_breakers_state: 'unavailable',
    },
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
