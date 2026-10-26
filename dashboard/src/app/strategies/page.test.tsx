import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buttonByText,
  click,
  dialogPolyfill,
  flush,
  inputByLabel,
  mount,
  routeFetch,
  sectionText,
  typeInto,
  unavailable,
  type Mounted,
  type Mutations,
  type Routes,
} from '@/test/pageHarness';
import { liveStatus, session } from '@/test/fixtures';
import { ApiError } from '@/lib/api';

const fetchApi = vi.fn();
const push = vi.fn();
const toast = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast }) }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push }) }));

const { default: StrategiesPage } = await import('./page');
const { ShellStatusProvider } = await import('@/components/shell/ShellStatusProvider');

const STRATEGY = {
  id: 'saw',
  name: 'Saw',
  description: 'Sawtooth',
  enabled: true,
  filename: 'saw.yaml',
  editable: true,
};

const PERFORMANCE = {
  total_pnl: 12,
  win_rate: 50,
  total_trades: 2,
  sharpe_ratio: 0.4,
  max_drawdown: 1,
};

const DRIFTED = {
  config_hash: 'aabb',
  file_changed: true,
  running_sessions: [{ session_id: 's1', config_hash: 'ccdd', stale: true }],
};

const RUNNING_SESSION = {
  session_id: 's1',
  strategy_id: 'saw',
  trading_mode: 'paper',
  status: 'running',
  started_at: null,
  stopped_at: null,
};

function baseRoutes(): Routes {
  return {
    '/api/strategies': [STRATEGY],
    '/api/trading/sessions': [],
    '/api/system/config': { trading_mode: 'paper', paper_capital: 2500 },
    '/api/strategies/saw/performance': PERFORMANCE,
  };
}

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

beforeEach(() => {
  dialogPolyfill();
  calls = [];
  push.mockReset();
  fetchApi.mockReset();
  toast.mockReset();
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

    const card = sectionText(page.container, 'Saw');
    expect(card).not.toContain('0.00Sharpe');
    expect(card).not.toContain('0.0%Max DD');
    expect(card).toContain('—Sharpe');
    expect(card).toContain('—Max DD');
  });

  it('shows PnL signed with USDT in the label, never a $ sign', async () => {
    const routes = baseRoutes();
    routes['/api/strategies/saw/performance'] = { ...PERFORMANCE, total_pnl: -50 };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    const card = sectionText(page.container, 'Saw');
    expect(card).toContain('−50.00');
    expect(card).toContain('PnL (USDT)');
    expect(card).not.toContain('$');
  });

  it('keeps PnL uncoloured with an sr-only gain/loss label for screen readers', async () => {
    const routes = baseRoutes();
    routes['/api/strategies/saw/performance'] = { ...PERFORMANCE, total_pnl: -50 };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    const labels = Array.from(page.container.querySelectorAll('.sr-only')).map((s) => s.textContent);
    expect(labels).toContain('loss 50.00 USDT');
    const value = Array.from(page.container.querySelectorAll('p')).find((p) => p.textContent?.includes('−50.00'));
    expect(value).toBeDefined();
    expect(value?.className).not.toContain('text-danger');
  });

  it('says Loading… with the ellipsis character while the list loads', async () => {
    const routes: Routes = { ...baseRoutes(), '/api/strategies': () => new Promise<never>(() => {}) };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    expect(page.container.textContent).toContain('Loading…');
    expect(page.container.textContent).not.toContain('Loading...');
  });

  it('asks for capital in the start dialog, not on the card', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<StrategiesPage />);

    expect(page.container.querySelector('input[type="number"]')).toBeNull();
  });

  it('does not invent a paper mode when the config is unavailable', async () => {
    const routes = baseRoutes();
    routes['/api/system/config'] = unavailable();
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    expect(page.container.querySelector('[data-testid="config-error"]')).not.toBeNull();
    expect(page.container.innerHTML).not.toContain('10000');
    expect(page.container.textContent).not.toContain('Start paper session');
  });

  it('hides session controls when sessions are unknown', async () => {
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = unavailable();
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    expect(page.container.querySelector('[data-testid="sessions-error"]')).not.toBeNull();
    expect(page.container.textContent).not.toContain('Start paper session');
  });
});

describe('StrategiesPage hot-reload state', () => {
  it('shows the caution row and the apply door when the file changed under a running session', async () => {
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = [RUNNING_SESSION];
    routes['/api/strategies/saw'] = DRIFTED;
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    const card = sectionText(page.container, 'Saw');
    expect(card).toContain('The strategy file changed after this session started.');
    expect(buttonByText(page.container, 'Apply to session…')).toBeTruthy();
  });

  it('shows neither when the running session is on the file it started with', async () => {
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = [RUNNING_SESSION];
    routes['/api/strategies/saw'] = {
      config_hash: 'aabb',
      file_changed: false,
      running_sessions: [{ session_id: 's1', config_hash: 'aabb', stale: false }],
    };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    const text = page.container.textContent ?? '';
    expect(text).not.toContain('The strategy file changed after this session started.');
    expect(text).not.toContain('Apply to session…');
  });

  it('renders a strategy whose detail fails instead of vanishing it', async () => {
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = [RUNNING_SESSION];
    routes['/api/strategies/saw'] = unavailable('Strategy detail could not be read');
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    const row = page.container.querySelector('[data-testid="drift-error"]');
    expect(row?.textContent).toContain('Strategy detail could not be read');
    expect(row?.querySelector('[data-state="unavailable"]')).not.toBeNull();
    expect(sectionText(page.container, 'Saw')).toContain('Sawtooth');
  });

  it('keeps the card with a NOT FOUND badge and the reason when the detail 404s between list and poll', async () => {
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = [RUNNING_SESSION];
    routes['/api/strategies/saw'] = new ApiError(
      404,
      JSON.stringify({ detail: { code: 'strategy_not_found', message: 'Strategy saw not found', state: 'not_found' } }),
    );
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    const row = page.container.querySelector('[data-testid="drift-error"]');
    expect(row?.textContent).toContain('NOT FOUND');
    expect(row?.textContent).toContain('Strategy saw not found');
    expect(row?.querySelector('[data-state="not_found"]')).not.toBeNull();
    expect(sectionText(page.container, 'Saw')).toContain('Sawtooth');
  });

  it('fetches the detail only for strategies with a running session (40 strategies, 2 running)', async () => {
    const many = Array.from({ length: 40 }, (_, i) => ({
      ...STRATEGY,
      id: `st-${String(i).padStart(2, '0')}`,
      name: `Strategy ${i}`,
    }));
    const routes: Routes = {
      '/api/strategies': many,
      '/api/trading/sessions': [
        { ...RUNNING_SESSION, strategy_id: 'st-07' },
        { ...RUNNING_SESSION, session_id: 's2', strategy_id: 'st-22' },
      ],
      '/api/system/config': { trading_mode: 'paper' },
      '/api/strategies/st-07': DRIFTED,
      '/api/strategies/st-22': DRIFTED,
    };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    const detailCalls = calls.filter((c) => /^\/api\/strategies\/st-\d\d$/.test(c.path));
    expect(detailCalls.map((c) => c.path).sort()).toEqual(['/api/strategies/st-07', '/api/strategies/st-22']);
    const perfCalls = calls.filter((c) => c.path.endsWith('/performance'));
    expect(perfCalls).toHaveLength(40);
  });
});

describe('StrategiesPage post-reload state', () => {
  const reloadOk = { session_id: 's1', from_hash: 'ccdd', to_hash: 'aabb', applies_at: 'next_closed_bar' };

  const LANDED = {
    config_hash: 'aabb',
    file_changed: false,
    running_sessions: [{ session_id: 's1', config_hash: 'aabb', stale: false }],
  };

  const STOPPED = { config_hash: 'aabb', file_changed: false, running_sessions: [] };

  async function applyReload() {
    const p = page;
    if (p === null) throw new Error('page not mounted');
    await click(buttonByText(p.container, 'Apply to session…'));
    await click(buttonByText(p.container, 'Apply to session'));
  }

  it('hides the caution row and the apply door while the applied reload is still pending', async () => {
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = [RUNNING_SESSION];
    routes['/api/strategies/saw'] = DRIFTED;
    fetchApi.mockImplementation(routeFetch(routes, calls, { 'POST /api/strategies/saw/reload': reloadOk }));
    page = await mount(<StrategiesPage />);

    await applyReload();

    const text = page.container.textContent ?? '';
    expect(text).toContain('Applied. The new parameters take effect at the next closed bar.');
    expect(text).not.toContain('The strategy file changed after this session started.');
    expect(
      Array.from(page.container.querySelectorAll('button')).map((b) => b.textContent?.trim()),
    ).not.toContain('Apply to session…');
  });

  it('clears the panel once the session reports the reloaded hash (the reload landed)', async () => {
    let detail: unknown = DRIFTED;
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = [RUNNING_SESSION];
    routes['/api/strategies/saw'] = () => detail;
    fetchApi.mockImplementation(routeFetch(routes, calls, { 'POST /api/strategies/saw/reload': reloadOk }));
    page = await mount(<StrategiesPage />);

    detail = LANDED;
    await applyReload();

    expect(page.container.querySelector('[data-testid="strategy-drift"]')).toBeNull();
    const text = page.container.textContent ?? '';
    expect(text).not.toContain('Applied.');
    expect(text).not.toContain('The strategy file changed after this session started.');
  });

  it('clears the panel when the session stops before the reload lands', async () => {
    let detail: unknown = DRIFTED;
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = [RUNNING_SESSION];
    routes['/api/strategies/saw'] = () => detail;
    fetchApi.mockImplementation(routeFetch(routes, calls, { 'POST /api/strategies/saw/reload': reloadOk }));
    page = await mount(<StrategiesPage />);

    detail = STOPPED;
    await applyReload();

    expect(page.container.querySelector('[data-testid="strategy-drift"]')).toBeNull();
    expect(page.container.textContent).not.toContain('Applied.');
  });
});

describe('StrategiesPage research hygiene', () => {
  it('has one h1, the phone Research nav, and no Start Live, Risk Overrides or Stop', async () => {
    const routes = baseRoutes();
    routes['/api/system/config'] = { trading_mode: 'live', paper_capital: 2500 };
    routes['/api/trading/sessions'] = [RUNNING_SESSION];
    routes['/api/strategies/saw'] = DRIFTED;
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    expect(page.container.querySelectorAll('h1')).toHaveLength(1);
    expect(page.container.querySelector('nav[aria-label="Research"] [aria-current="page"]')?.textContent).toBe('Strategies');
    const text = page.container.textContent ?? '';
    expect(text).not.toContain('Start Live');
    expect(text).not.toContain('Risk Overrides');
    expect(Array.from(page.container.querySelectorAll('button')).map((b) => b.textContent?.trim())).not.toContain('Stop');
    expect(calls.some((c) => c.path.startsWith('/api/risk/config'))).toBe(false);
  });

  it('offers a paper session only in paper mode', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<StrategiesPage />);
    expect(page.container.textContent).toContain('Start paper session');
    page.unmount();

    const routes = baseRoutes();
    routes['/api/system/config'] = { trading_mode: 'live', paper_capital: 2500 };
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);
    expect(page.container.textContent).not.toContain('Start paper session');
  });

  it('says what to do when there are no strategies', async () => {
    const routes = baseRoutes();
    routes['/api/strategies'] = [];
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    expect(page.container.textContent).toContain('No strategies yet. Build one or start from a template.');
    const labels = Array.from(page.container.querySelectorAll('button')).map((b) => b.textContent?.trim());
    expect(labels).toContain('New strategy');
    expect(labels).toContain('New from template');
  });
});

describe('StrategiesPage start paper session', () => {
  it('opens the start dialog and sends nothing until it is confirmed', async () => {
    const routes: Routes = {
      ...baseRoutes(),
      '/api/risk/live-status': liveStatus(),
      '/api/strategies/saw': { id: 'saw', name: 'Saw', symbol: 'BTCUSDT', timeframes: { primary: '1h' } },
    };
    const mutations: Mutations = {
      'POST /api/trading/sessions': session({ session_id: 'new-1', strategy_id: 'saw', paper_capital: 6000 }),
    };
    fetchApi.mockImplementation(routeFetch(routes, calls, mutations));
    page = await mount(
      <ShellStatusProvider>
        <StrategiesPage />
      </ShellStatusProvider>,
    );

    await click(buttonByText(page.container, 'Start paper session'));
    const dialog = page.container.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(dialog.textContent).toContain('Start a paper session');
    expect(calls.filter((c) => c.init?.method === 'POST')).toHaveLength(0);

    await typeInto(inputByLabel(dialog, 'Paper capital for this session (USDT)'), '8000');
    await click(buttonByText(dialog, 'Start paper session'));
    const post = calls.find((c) => c.init?.method === 'POST');
    expect(post?.path).toBe('/api/trading/sessions');
    expect(JSON.parse(post?.init?.body as string)).toEqual({
      strategy_id: 'saw',
      trading_mode: 'paper',
      paper_capital: '8000',
    });
    expect(push).toHaveBeenCalledWith('/trading/new-1');
  });
});

describe('StrategiesPage delete', () => {
  it('asks via a confirm dialog focused on its title and deletes once after confirming', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm');
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls, {}));
    page = await mount(<StrategiesPage />);

    await click(buttonByText(page.container, 'Delete'));
    expect(confirmSpy).not.toHaveBeenCalled();
    const dialog = page.container.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(document.activeElement?.textContent).toBe('Delete Saw?');
    expect(document.activeElement?.tagName).not.toBe('BUTTON');
    expect(dialog.textContent).toContain("This removes the strategy's YAML file. This cannot be undone.");

    const primary = buttonByText(dialog, 'Delete strategy');
    await act(async () => {
      primary.click();
      primary.click();
    });
    await flush();
    const deletes = calls.filter((c) => c.path === '/api/strategies/saw' && c.init?.method === 'DELETE');
    expect(deletes).toHaveLength(1);
    expect(page.container.querySelector('dialog[open]')).toBeNull();
    confirmSpy.mockRestore();
  });

  it('keeps the strategy when the secondary is chosen', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls, {}));
    page = await mount(<StrategiesPage />);

    await click(buttonByText(page.container, 'Delete'));
    await click(buttonByText(page.container, 'Keep it'));
    expect(calls.filter((c) => c.init?.method === 'DELETE')).toHaveLength(0);
    expect(page.container.querySelector('dialog[open]')).toBeNull();
  });

  it('says it could not check for a running session when the sessions poll is unavailable', async () => {
    const routes = baseRoutes();
    routes['/api/trading/sessions'] = unavailable();
    fetchApi.mockImplementation(routeFetch(routes, calls));
    page = await mount(<StrategiesPage />);

    await click(buttonByText(page.container, 'Delete'));
    const dialog = page.container.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(dialog.textContent).toContain("Couldn't check for a running session.");
    expect(dialog.textContent).not.toContain('No session is running on it.');
  });

  it('refreshes the sessions when the delete dialog opens', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls, {}));
    page = await mount(<StrategiesPage />);

    const before = calls.filter((c) => c.path === '/api/trading/sessions').length;
    expect(before).toBeGreaterThan(0);
    await click(buttonByText(page.container, 'Delete'));
    const dialog = page.container.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(calls.filter((c) => c.path === '/api/trading/sessions').length).toBe(before + 1);
    expect(dialog.textContent).toContain('No session is running on it.');
  });
});

describe('StrategiesPage import', () => {
  it('sends the chosen file as multipart FormData, not JSON', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls, { 'POST /api/strategies/import': {} }));
    page = await mount(<StrategiesPage />);

    const input = page.container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['id: imported\nname: Imported\n'], 'my.yaml', { type: 'application/x-yaml' });
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    await act(async () => {
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await flush();

    const sent = calls.find((c) => c.path === '/api/strategies/import');
    expect(sent?.init?.method).toBe('POST');
    const body = sent?.init?.body as FormData;
    expect(body).toBeInstanceOf(FormData);
    const uploaded = body.get('file');
    expect(uploaded).toBeInstanceOf(Blob);
    expect((uploaded as File).name).toBe('my.yaml');
  });
});

describe('StrategiesPage phone-equal targets', () => {
  it('gives the four card actions and the enable switch a 44 px target below 1024', async () => {
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<StrategiesPage />);

    const actions = Array.from(page.container.querySelectorAll('button')).filter((b) =>
      ['Edit', 'Backtest', 'Export', 'Delete'].includes(b.textContent?.trim() ?? ''),
    );
    expect(actions).toHaveLength(4);
    for (const action of actions) expect(action.className).toContain('max-lg:h-11');

    const target = page.container.querySelector('[role="switch"]');
    expect(target?.className).toContain('max-lg:min-h-11');
  });
});

describe('StrategiesPage export', () => {
  const exportFetch = vi.fn();
  const createObjectURL = vi.fn<(blob: Blob) => string>(() => 'blob:export');
  const revokeObjectURL = vi.fn();
  const anchorClicks: HTMLAnchorElement[] = [];
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;
  const originalAnchorClick = HTMLAnchorElement.prototype.click;

  // jsdom has no downloads: record anchor clicks instead of letting them navigate.
  function recordAnchorClick(this: HTMLAnchorElement): void {
    anchorClicks.push(this);
  }

  beforeEach(() => {
    URL.createObjectURL = createObjectURL;
    URL.revokeObjectURL = revokeObjectURL;
    HTMLAnchorElement.prototype.click = recordAnchorClick;
    vi.stubGlobal('fetch', exportFetch);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
    HTMLAnchorElement.prototype.click = originalAnchorClick;
    anchorClicks.length = 0;
    createObjectURL.mockClear();
    revokeObjectURL.mockClear();
    exportFetch.mockReset();
  });

  it('downloads the YAML via a raw-text fetch and does not report a JSON-parse failure', async () => {
    exportFetch.mockResolvedValue(
      new Response('id: saw\nname: Saw\n', { status: 200, headers: { 'content-type': 'application/x-yaml' } }),
    );
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<StrategiesPage />);

    await click(buttonByText(page.container, 'Export'));

    expect(String(exportFetch.mock.calls[0]?.[0])).toContain('/api/strategies/saw/export');
    expect(calls.some((c) => c.path === '/api/strategies/saw/export')).toBe(false);
    expect(toast).toHaveBeenCalledWith('success', 'Exported "Saw"');
    expect(toast).not.toHaveBeenCalledWith('error', 'Failed to export strategy');
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const blob = createObjectURL.mock.calls[0]?.[0];
    expect(blob).toBeInstanceOf(Blob);
    expect(blob?.type).toBe('application/x-yaml');
    expect(anchorClicks).toHaveLength(1);
    expect(anchorClicks[0]?.download).toBe('saw.yaml');
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:export');
  });

  it('toasts the server message and downloads nothing when the export fails', async () => {
    exportFetch.mockResolvedValue(
      new Response(
        JSON.stringify({ detail: { code: 'strategy_not_found', message: "Strategy 'saw' not found", state: 'not_found' } }),
        { status: 404 },
      ),
    );
    fetchApi.mockImplementation(routeFetch(baseRoutes(), calls));
    page = await mount(<StrategiesPage />);

    await click(buttonByText(page.container, 'Export'));

    expect(toast).toHaveBeenCalledWith('error', "Strategy 'saw' not found");
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(anchorClicks).toHaveLength(0);
  });
});
