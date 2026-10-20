import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, routeFetch, unavailable, type Mounted, type Routes } from '@/test/pageHarness';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));

const { default: StrategiesPage } = await import('./page');

const STRATEGY = {
  id: 'saw',
  name: 'Saw',
  description: 'Sawtooth',
  enabled: true,
  filename: 'saw.yaml',
  editable: true,
};

function baseRoutes(): Routes {
  return {
    '/api/strategies': [STRATEGY],
    '/api/trading/sessions': [],
    '/api/system/config': { trading_mode: 'paper', paper_capital: 2500 },
    '/api/strategies/saw/performance': {
      total_pnl: 12,
      win_rate: 50,
      total_trades: 2,
      sharpe_ratio: 0.4,
      max_drawdown: 1,
    },
  };
}

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

beforeEach(() => {
  calls = [];
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('StrategiesPage error states', () => {
  it('shows an outage instead of "0 active strategies" when the list is unavailable', async () => {
    const routes = baseRoutes();
    routes['/api/strategies'] = unavailable();
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Strategies unavailable');
    expect(text).toContain('Database is not available');
    expect(text).not.toContain('0 active strategies');
    expect(page.container.querySelector('[data-state="unavailable"]')).not.toBeNull();
  });

  it('marks performance as unavailable instead of hiding it', async () => {
    const routes = baseRoutes();
    routes['/api/strategies/saw/performance'] = unavailable('Database error while loading strategy performance');
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    const row = page.container.querySelector('[data-testid="performance-error"]');
    expect(row?.textContent).toContain('Database error while loading strategy performance');
    expect(row?.querySelector('[data-state="unavailable"]')).not.toBeNull();
  });

  it('shows uncomputed Sharpe and drawdown as unknown, not zero', async () => {
    const routes = baseRoutes();
    routes['/api/strategies/saw/performance'] = {
      total_pnl: 0,
      win_rate: 0,
      total_trades: 0,
      sharpe_ratio: null,
      max_drawdown: null,
    };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    const text = page.container.textContent ?? '';
    expect(text).not.toContain('0.00Sharpe');
    expect(text).not.toContain('0.0%Max DD');
    expect(text).toContain('—Sharpe');
    expect(text).toContain('—Max DD');
  });

  it('uses the configured paper capital', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<StrategiesPage />);

    const capital = page.container.querySelector<HTMLInputElement>('input[type="number"]');
    expect(capital?.value).toBe('2500');
  });

  it('does not invent a 10000 capital or a paper mode when the config is unavailable', async () => {
    const routes = baseRoutes();
    routes['/api/system/config'] = unavailable();
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    expect(page.container.querySelector('[data-testid="config-error"]')).not.toBeNull();
    expect(page.container.innerHTML).not.toContain('10000');
    expect(page.container.textContent).not.toContain('Start Paper');
  });

  it('hides session controls when sessions are unknown', async () => {
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = unavailable();
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    expect(page.container.querySelector('[data-testid="sessions-error"]')).not.toBeNull();
    expect(page.container.textContent).not.toContain('Start Paper');
  });
});
