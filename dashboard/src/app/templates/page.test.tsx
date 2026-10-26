import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buttonByText, click, mount, routeFetch, sectionText, unavailable, type Mounted, type Mutations, type Routes } from '@/test/pageHarness';

const fetchApi = vi.fn();
const push = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push }) }));

const { default: TemplatesPage } = await import('./page');

const TEMPLATE = {
  id: 'momentum_rsi',
  name: 'RSI Momentum',
  description: 'Classic RSI oversold/overbought strategy.',
  type: 'rule_based',
  risk_level: 'conservative',
  expected_sharpe: null,
  expected_max_drawdown: null,
  recommended_capital: null,
  symbols: ['BTCUSDT'],
  timeframe: '1h',
};

function baseRoutes(): Routes {
  return { '/api/templates': [TEMPLATE] };
}

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

beforeEach(() => {
  calls = [];
  push.mockReset();
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('TemplatesPage error states', () => {
  it('shows an outage with Retry instead of "No templates available yet" when the list is unavailable', async () => {
    const routes = baseRoutes();
    routes['/api/templates'] = unavailable('Templates could not be read');
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TemplatesPage />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('Templates unavailable');
    expect(text).toContain('Templates could not be read');
    expect(text).not.toContain('No templates available yet');
    expect(page.container.querySelector('[data-state="unavailable"]')).not.toBeNull();

    const retriesBefore = calls.filter((c) => c.path === '/api/templates').length;
    await click(buttonByText(page.container, 'Retry'));
    expect(calls.filter((c) => c.path === '/api/templates').length).toBe(retriesBefore + 1);
  });

  it('shows an empty state only when the list loaded with no templates', async () => {
    const routes = baseRoutes();
    routes['/api/templates'] = [];
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TemplatesPage />);

    expect(page.container.textContent).toContain('No templates available yet');
    expect(page.container.querySelector('[data-state="unavailable"]')).toBeNull();
  });
});

describe('TemplatesPage content', () => {
  it('has one h1, the phone Research nav, and the real template fields', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<TemplatesPage />);

    expect(page.container.querySelectorAll('h1')).toHaveLength(1);
    expect(page.container.querySelector('h1')?.textContent).toBe('Templates');
    expect(page.container.querySelector('nav[aria-label="Research"] [aria-current="page"]')?.textContent).toBe('Templates');
    const text = page.container.textContent ?? '';
    expect(text).toContain('RSI Momentum');
    expect(text).toContain('BTCUSDT · 1h');
    expect(text).toContain('Rule-based');
    expect(text).toContain('Conservative');
    expect(text).not.toContain('conservative');
    expect(text).not.toContain('by ');
    expect(page.container.querySelector('.bg-caution-soft, .bg-danger-soft, .text-caution, .text-danger')).toBeNull();
  });

  it('marks unset expectations as NO DATA, never as zero', async () => {
    const routes = baseRoutes();
    routes['/api/templates'] = [
      { ...TEMPLATE },
      {
        ...TEMPLATE,
        id: 'breakout_volume',
        name: 'Bollinger Breakout',
        expected_sharpe: 1.2,
        expected_max_drawdown: 15,
        recommended_capital: 10000,
      },
    ];
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<TemplatesPage />);

    expect(page.container.querySelectorAll('[data-state="no_data"]')).toHaveLength(3);
    const rsi = sectionText(page.container, 'RSI Momentum');
    expect(rsi).toContain('—');
    expect(rsi).toContain('NO DATA');
    expect(rsi).not.toContain('0.00');
    const breakout = sectionText(page.container, 'Bollinger Breakout');
    expect(breakout).toContain('1.20');
    expect(breakout).toContain('15.00%');
    expect(breakout).not.toContain('15.0%');
    expect(breakout).toContain('10,000.00');
    expect(breakout).not.toContain('NO DATA');
  });

  it('imports a template and opens it in the builder by the returned id', async () => {
    const mutations: Mutations = {
      'POST /api/templates/momentum_rsi/import': {
        id: 'momentum_rsi_ab12cd',
        name: 'RSI Momentum',
        config_path: '/config/strategies/momentum_rsi_ab12cd.yaml',
        message: "Template 'momentum_rsi' imported as 'momentum_rsi_ab12cd'",
      },
    };
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls, mutations));
    page = await mount(<TemplatesPage />);

    await click(buttonByText(page.container, 'Import'));
    const post = calls.find((c) => c.init?.method === 'POST');
    expect(post?.path).toBe('/api/templates/momentum_rsi/import');
    expect(push).toHaveBeenCalledWith('/builder?strategy=momentum_rsi_ab12cd');
  });
});
