import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { ApiError } from '@/lib/api';
import { buttonByText, click, dialogPolyfill, flush, mount, routeFetch, sectionText, unavailable, type Mounted, type Routes } from '@/test/pageHarness';

const fetchApi = vi.fn();
const toast = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast }) }));
let searchParams = new URLSearchParams('strategy=strat-1');
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

/** A saved perp strategy as the detail read answers it (the leverage pass-through, P4-F). */
const PERP_STRATEGY = { ...STRATEGY, exchange_id: 'binanceusdm', leverage: 2 };

function loadedPerpRoutes(): Routes {
  return { ...routes(), '/api/strategies/strat-1': PERP_STRATEGY };
}

const byText = (text: string) =>
  Array.from(page!.container.querySelectorAll('button')).filter((b) => b.textContent?.trim() === text);

const initialHref = window.location.href;
const BUILDER_URL = '/builder?strategy=strat-1';

/** A popstate the way a traversal delivers it: the URL already changed, the event follows. */
async function popstate(): Promise<void> {
  await act(async () => {
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await flush();
}

beforeEach(() => {
  dialogPolyfill();
  calls = [];
  fetchApi.mockReset();
  toast.mockReset();
  router.push.mockReset();
  searchParams = new URLSearchParams('strategy=strat-1');
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

    await click(buttonByText(page.container, 'Update strategy'));
    await click(buttonByText(page.container, 'Save strategy'));

    const put = calls.find((c) => c.init?.method === 'PUT');
    expect(JSON.parse(String(put?.init?.body)).ml_overlay).toEqual({ model_name: 'm1', confidence_threshold: 0.6 });
  });

  it('round-trips a loaded perp strategy\u2019s leverage through the dialog\u2019s PUT (P4-F)', async () => {
    fetchApi.mockImplementation(routeFetch(loadedPerpRoutes(), calls, { 'PUT /api/strategies/strat-1': { id: 'strat-1' } }));
    page = await mount(<BuilderPage />);

    await flush();
    await click(buttonByText(page.container, 'Update strategy'));
    const dialog = page.container.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.textContent).toContain('Perps (Binance USD⌂-M)');
    await click(buttonByText(dialog, 'Save strategy'));

    const put = calls.find((c) => c.init?.method === 'PUT');
    expect(JSON.parse(String(put?.init?.body))).toMatchObject({ exchange_id: 'binanceusdm', leverage: 2 });
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

    await click(buttonByText(page.container, 'Update strategy'));
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
    window.history.pushState(null, '', initialHref);
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

  it('intercepts keyboard-activated navigation (Enter on a focused nav link)', async () => {
    fetchApi.mockImplementation(routeFetch(loadedRoutes(), calls));
    window.history.pushState(null, '', BUILDER_URL);
    page = await mount(<BuilderPage />);
    await click(buttonByText(page.container, 'Add Condition'));

    const nav = document.createElement('a');
    nav.setAttribute('href', '/strategies');
    nav.textContent = 'Strategies';
    page.container.appendChild(nav);
    nav.focus();

    // Enter on a link fires an activation click: button 0, detail 0
    await act(async () => {
      nav.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      nav.dispatchEvent(
        new MouseEvent('click', { button: 0, detail: 0, bubbles: true, cancelable: true }),
      );
    });
    await flush();

    const dialog = page.container.querySelector('dialog[open]') as HTMLDialogElement;
    expect(dialog.textContent).toContain('Discard unsaved changes?');
    expect(router.push).not.toHaveBeenCalled();
  });

  it('does not intercept nav clicks after a save makes the draft clean', async () => {
    fetchApi.mockImplementation(
      routeFetch(loadedRoutes(), calls, { 'PUT /api/strategies/strat-1': { id: 'strat-1' } }),
    );
    page = await mount(<BuilderPage />);
    await click(buttonByText(page.container, 'Add Condition'));

    const nav = document.createElement('a');
    nav.setAttribute('href', '/strategies');
    nav.textContent = 'Strategies';
    page.container.appendChild(nav);

    await click(buttonByText(page.container, 'Update strategy'));
    await click(buttonByText(page.container, 'Save strategy'));

    await click(nav);
    expect(page.container.querySelector('dialog[open]')).toBeNull();
    expect(router.push).not.toHaveBeenCalled();
  });
});

describe('BuilderPage history-traversal guard (S1 amendment)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.pushState(null, '', initialHref);
  });

  it('traps a back traversal over a dirty draft and keeps it on the page when cancelled', async () => {
    fetchApi.mockImplementation(routeFetch(loadedRoutes(), calls));
    window.history.pushState({ marker: 'armed' }, '', BUILDER_URL);
    page = await mount(<BuilderPage />);
    await click(buttonByText(page.container, 'Add Condition'));

    const builderHref = window.location.href;
    window.history.pushState(null, '', '/strategies'); // the browser traversed away
    const pushSpy = vi.spyOn(window.history, 'pushState');
    await popstate();

    const dialog = page.container.querySelector('dialog[open]') as HTMLDialogElement;
    expect(dialog.textContent).toContain('Discard unsaved changes?');
    expect(pushSpy).toHaveBeenCalledWith({ marker: 'armed' }, '', builderHref);
    expect(window.location.pathname).toBe('/builder');
    expect(window.location.search).toBe('?strategy=strat-1');
    expect(sectionText(page.container, 'Entry Long')).toContain('1 condition');
    expect(router.push).not.toHaveBeenCalled();

    await click(buttonByText(page.container, 'Keep editing'));
    expect(page.container.querySelector('dialog[open]')).toBeNull();
    expect(window.location.pathname).toBe('/builder');
    expect(sectionText(page.container, 'Entry Long')).toContain('1 condition');
  });

  it('performs the pending traversal when the discard is confirmed', async () => {
    fetchApi.mockImplementation(routeFetch(loadedRoutes(), calls));
    window.history.pushState({ marker: 'armed' }, '', BUILDER_URL);
    page = await mount(<BuilderPage />);
    await click(buttonByText(page.container, 'Add Condition'));

    window.history.pushState(null, '', '/strategies');
    await popstate();

    await click(buttonByText(page.container, 'Discard changes'));
    expect(router.push).toHaveBeenCalledWith('/strategies');
    expect(page.container.querySelector('dialog[open]')).toBeNull();
    expect(sectionText(page.container, 'Entry Long')).toContain('0 conditions');
  });

  it('removes the popstate guard once a save makes the draft clean', async () => {
    const addSpy = vi.spyOn(window, 'addEventListener');
    const removeSpy = vi.spyOn(window, 'removeEventListener');
    const added = () => addSpy.mock.calls.filter((c) => c[0] === 'popstate');
    const removed = () => removeSpy.mock.calls.filter((c) => c[0] === 'popstate');
    fetchApi.mockImplementation(
      routeFetch(loadedRoutes(), calls, { 'PUT /api/strategies/strat-1': { id: 'strat-1' } }),
    );
    window.history.pushState({ marker: 'armed' }, '', BUILDER_URL);
    page = await mount(<BuilderPage />);
    expect(added()).toHaveLength(0);

    await click(buttonByText(page.container, 'Add Condition'));
    expect(added()).toHaveLength(1);

    await click(buttonByText(page.container, 'Update strategy'));
    await click(buttonByText(page.container, 'Save strategy'));
    expect(removed()).toHaveLength(1);

    window.history.pushState(null, '', '/strategies');
    await popstate();
    expect(page.container.querySelector('dialog[open]')).toBeNull();
  });

  it('does not load a traversed-to ?strategy over a dirty draft', async () => {
    const r = loadedRoutes();
    r['/api/strategies/strat-2'] = { ...STRATEGY, id: 'strat-2', name: 'Saw 2' };
    fetchApi.mockImplementation(routeFetch(r, calls));
    window.history.pushState({ marker: 'armed' }, '', BUILDER_URL);
    page = await mount(<BuilderPage />);
    await click(buttonByText(page.container, 'Add Condition'));

    // the traversal committed ?strategy=strat-2 to the router before the guard restored the URL
    searchParams = new URLSearchParams('strategy=strat-2');
    window.history.pushState(null, '', '/builder?strategy=strat-2');
    await popstate();
    await click(buttonByText(page.container, 'Add Condition'));

    expect(page.container.querySelector('dialog[open]')).not.toBeNull();
    expect(window.location.search).toBe('?strategy=strat-1');
    expect(calls.some((c) => c.path === '/api/strategies/strat-2')).toBe(false);
    expect(sectionText(page.container, 'Entry Long')).toContain('2 conditions');
  });
});

describe('BuilderPage same-route strategy switch (S1)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.pushState(null, '', initialHref);
  });

  it('confirms before an in-page anchor changes ?strategy over a dirty draft', async () => {
    fetchApi.mockImplementation(routeFetch(loadedRoutes(), calls));
    window.history.pushState(null, '', BUILDER_URL);
    page = await mount(<BuilderPage />);
    await click(buttonByText(page.container, 'Add Condition'));

    const nav = document.createElement('a');
    nav.setAttribute('href', '/builder?strategy=strat-2');
    nav.textContent = 'Saw 2';
    page.container.appendChild(nav);

    await click(nav);
    const dialog = page.container.querySelector('dialog[open]') as HTMLDialogElement;
    expect(dialog.textContent).toContain('Discard unsaved changes?');
    expect(router.push).not.toHaveBeenCalled();
    expect(sectionText(page.container, 'Entry Long')).toContain('1 condition');

    await click(buttonByText(page.container, 'Discard changes'));
    expect(router.push).toHaveBeenCalledWith('/builder?strategy=strat-2');
  });
});
