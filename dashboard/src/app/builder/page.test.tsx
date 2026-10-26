import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, dialogPolyfill, mount, routeFetch, sectionText, unavailable, type Mounted, type Routes } from '@/test/pageHarness';

const fetchApi = vi.fn();
const toast = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast }) }));
const searchParams = new URLSearchParams('strategy=strat-1');
const router = { push: vi.fn() };
vi.mock('next/navigation', () => ({
  useRouter: () => router,
  useSearchParams: () => searchParams,
}));

const { default: BuilderPage } = await import('./page');

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

function routes(): Routes {
  return {
    '/api/strategies/indicators': [],
    '/api/strategies/comparators': [],
    '/api/strategies/strat-1': new ApiError(
      404,
      JSON.stringify({
        detail: { code: 'strategy_not_found', message: 'Strategy strat-1 not found', state: 'not_found' },
      }),
    ),
  };
}

const STRATEGY = {
  id: 'strat-1',
  name: 'Saw',
  description: 'd',
  enabled: false,
  exchange_id: 'binance',
  symbol: 'BTCUSDT',
  rules: { entry_long: null, exit_long: null, entry_short: null, exit_short: null },
  timeframes: { primary: '1h' },
  risk: {
    stop_loss_method: 'atr',
    stop_loss_value: 2,
    take_profit_method: 'atr',
    take_profit_value: 3,
    sizing_method: 'fixed_fractional',
    sizing_params: {},
  },
};

function loadedRoutes(): Routes {
  return { ...routes(), '/api/strategies/strat-1': STRATEGY };
}

const byText = (text: string) =>
  Array.from(page!.container.querySelectorAll('button')).filter((b) => b.textContent?.trim() === text);

beforeEach(() => {
  dialogPolyfill();
  calls = [];
  fetchApi.mockReset();
  toast.mockReset();
  router.push.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('BuilderPage error paths', () => {
  it('shows the 404 for an unknown strategy instead of the raw response body', async () => {
    fetchApi.mockImplementation(routeFetch(routes(), calls));
    page = await mount(<BuilderPage />);

    const card = page.container.querySelector('[data-testid="load-error"]');
    expect(card?.textContent).toContain('Strategy strat-1 not found');
    expect(card?.querySelector('[data-state="not_found"]')).not.toBeNull();
    expect(toast).toHaveBeenCalledWith('error', 'Failed to load strategy: Strategy strat-1 not found');
  });

  it('says when the indicator catalogue falls back to the built-in list', async () => {
    const r = routes();
    r['/api/strategies/indicators'] = unavailable();
    fetchApi.mockImplementation(routeFetch(r, calls));
    page = await mount(<BuilderPage />);

    expect(toast).toHaveBeenCalledWith(
      'warning',
      'Using built-in indicator list; server catalogue unavailable (indicators: Database is not available)',
    );
  });
});

describe('BuilderPage scope', () => {
  it('has one h1 and no DCA, Grid or ML overlay controls', async () => {
    fetchApi.mockImplementation(routeFetch(routes(), calls));
    page = await mount(<BuilderPage />);

    expect(page.container.querySelectorAll('h1')).toHaveLength(1);
    const text = page.container.textContent ?? '';
    expect(text).not.toContain('DCA');
    expect(text).not.toContain('Grid Bot');
    expect(text).not.toContain('ML Model Overlay');
    expect(calls.some((c) => c.path === '/api/models')).toBe(false);
  });

  it('keeps ml_overlay when a strategy that has one is saved', async () => {
    const r = routes();
    r['/api/strategies/strat-1'] = {
      id: 'strat-1',
      name: 'Saw',
      description: 'd',
      enabled: false,
      exchange_id: 'binance',
      symbol: 'BTCUSDT',
      rules: { entry_long: null, exit_long: null, entry_short: null, exit_short: null },
      timeframes: { primary: '1h' },
      risk: {
        stop_loss_method: 'atr',
        stop_loss_value: 2,
        take_profit_method: 'atr',
        take_profit_value: 3,
        sizing_method: 'fixed_fractional',
        sizing_params: {},
      },
      ml_overlay: { model_name: 'm1', confidence_threshold: 0.6 },
    };
    fetchApi.mockImplementation(routeFetch(r, calls, { 'PUT /api/strategies/strat-1': { id: 'strat-1' } }));
    page = await mount(<BuilderPage />);

    await click(buttonByText(page.container, 'Update Strategy'));
    await click(buttonByText(page.container, 'Save strategy'));

    const put = calls.find((c) => c.init?.method === 'PUT');
    expect(JSON.parse(String(put?.init?.body)).ml_overlay).toEqual({ model_name: 'm1', confidence_threshold: 0.6 });
  });
});

describe('BuilderPage draft guard', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('registers beforeunload only while the draft is dirty and removes it after a save', async () => {
    const addSpy = vi.spyOn(window, 'addEventListener');
    const removeSpy = vi.spyOn(window, 'removeEventListener');
    const added = () => addSpy.mock.calls.filter((c) => c[0] === 'beforeunload');
    const removed = () => removeSpy.mock.calls.filter((c) => c[0] === 'beforeunload');
    fetchApi.mockImplementation(
      routeFetch(loadedRoutes(), calls, { 'PUT /api/strategies/strat-1': { id: 'strat-1' } }),
    );
    page = await mount(<BuilderPage />);

    expect(added()).toHaveLength(0);

    await click(buttonByText(page.container, 'Add Condition'));
    expect(added()).toHaveLength(1);

    await click(buttonByText(page.container, 'Update Strategy'));
    await click(buttonByText(page.container, 'Save strategy'));
    expect(removed()).toHaveLength(1);
  });

  it('confirms before New Strategy resets a dirty draft, and keeps the draft on cancel', async () => {
    fetchApi.mockImplementation(routeFetch(loadedRoutes(), calls));
    page = await mount(<BuilderPage />);
    await click(buttonByText(page.container, 'Add Condition'));

    await click(buttonByText(page.container, 'New Strategy'));
    const dialog = page.container.querySelector('dialog[open]') as HTMLDialogElement;
    expect(dialog.textContent).toContain('Discard unsaved changes?');
    expect(document.activeElement?.textContent).toBe('Discard unsaved changes?');
    expect(document.activeElement?.tagName).not.toBe('BUTTON');

    await click(buttonByText(page.container, 'Keep editing'));
    expect(page.container.querySelector('dialog[open]')).toBeNull();
    expect(sectionText(page.container, 'Entry Long')).toContain('1 condition');

    await click(buttonByText(page.container, 'New Strategy'));
    await click(buttonByText(page.container, 'Discard changes'));
    expect(page.container.querySelector('dialog[open]')).toBeNull();
    expect(byText('New Strategy')).toHaveLength(0);
    expect(sectionText(page.container, 'Entry Long')).toContain('0 conditions');
  });
});

describe('BuilderPage in-app route-leave guard (S1)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('routes nav-link clicks through the discard confirm while dirty; cancel keeps the draft', async () => {
    fetchApi.mockImplementation(routeFetch(loadedRoutes(), calls));
    page = await mount(<BuilderPage />);
    await click(buttonByText(page.container, 'Add Condition'));

    const nav = document.createElement('a');
    nav.setAttribute('href', '/strategies');
    nav.textContent = 'Strategies';
    page.container.appendChild(nav);

    await click(nav);
    const dialog = page.container.querySelector('dialog[open]') as HTMLDialogElement;
    expect(dialog.textContent).toContain('Discard unsaved changes?');
    expect(router.push).not.toHaveBeenCalled();
    expect(sectionText(page.container, 'Entry Long')).toContain('1 condition');

    await click(buttonByText(page.container, 'Keep editing'));
    expect(router.push).not.toHaveBeenCalled();
    expect(page.container.querySelector('dialog[open]')).toBeNull();

    await click(nav);
    await click(buttonByText(page.container, 'Discard changes'));
    expect(router.push).toHaveBeenCalledWith('/strategies');
    expect(page.container.querySelector('dialog[open]')).toBeNull();
  });

  it('does not intercept nav links while the draft is clean', async () => {
    fetchApi.mockImplementation(routeFetch(loadedRoutes(), calls));
    page = await mount(<BuilderPage />);

    const nav = document.createElement('a');
    nav.setAttribute('href', '/strategies');
    nav.textContent = 'Strategies';
    page.container.appendChild(nav);

    await click(nav);
    expect(page.container.querySelector('dialog[open]')).toBeNull();
    expect(router.push).not.toHaveBeenCalled();
  });
});
