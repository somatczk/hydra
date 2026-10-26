import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import {
  buttonByText,
  click,
  dialogPolyfill,
  mount,
  routeFetch,
  type Mounted,
  type Mutations,
} from '@/test/pageHarness';
import { StrategyDriftPanel, type StrategyDrift } from './StrategyDriftPanel';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const DRIFTED: StrategyDrift = {
  config_hash: 'aabb',
  file_changed: true,
  running_sessions: [{ session_id: 's1', config_hash: 'ccdd', stale: true }],
};

const CLEAN: StrategyDrift = {
  config_hash: 'aabb',
  file_changed: false,
  running_sessions: [{ session_id: 's1', config_hash: 'aabb', stale: false }],
};

const reloads = () => calls.filter((c) => c.path === '/api/strategies/saw/reload' && c.init?.method === 'POST');

let m: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;
let refetches: number;

const refetch = () => {
  refetches += 1;
};

function setup(mutations: Mutations = {}) {
  fetchApi.mockImplementation(routeFetch({}, calls, mutations));
}

beforeEach(() => {
  dialogPolyfill();
  calls = [];
  refetches = 0;
});

afterEach(() => {
  m?.unmount();
  m = null;
  fetchApi.mockReset();
});

describe('StrategyDriftPanel', () => {
  it('shows the caution row and the apply door only when the file changed', async () => {
    m = await mount(<StrategyDriftPanel strategyId="saw" drift={DRIFTED} onRefetch={refetch} />);
    expect(m.container.textContent).toContain('The strategy file changed after this session started.');
    expect(buttonByText(m.container, 'Apply to session…')).toBeTruthy();

    m?.unmount();
    m = await mount(<StrategyDriftPanel strategyId="saw" drift={CLEAN} onRefetch={refetch} />);
    expect(m.container.querySelector('[data-testid="strategy-drift"]')).toBeNull();
    expect(m.container.textContent).not.toContain('The strategy file changed after this session started.');
  });

  it('shows the caution row when only a running session is stale', async () => {
    m = await mount(
      <StrategyDriftPanel
        strategyId="saw"
        drift={{ ...CLEAN, running_sessions: [{ session_id: 's1', config_hash: 'ccdd', stale: true }] }}
        onRefetch={refetch}
      />,
    );
    expect(m.container.textContent).toContain('The strategy file changed after this session started.');
  });

  it('opens the confirm focused on its title with the restart facts', async () => {
    setup();
    m = await mount(<StrategyDriftPanel strategyId="saw" drift={DRIFTED} onRefetch={refetch} />);
    await click(buttonByText(m.container, 'Apply to session…'));
    const dialog = m.container.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(document.activeElement?.textContent).toBe('Apply the changed parameters?');
    expect(document.activeElement?.tagName).not.toBe('BUTTON');
    expect(dialog.textContent).toContain('take effect at the next closed bar');
    expect(dialog.textContent).toContain('they need a session restart');
  });

  it('keeps the session on its snapshot when the secondary is chosen', async () => {
    setup();
    m = await mount(<StrategyDriftPanel strategyId="saw" drift={DRIFTED} onRefetch={refetch} />);
    await click(buttonByText(m.container, 'Apply to session…'));
    await click(buttonByText(m.container, 'Not now'));
    expect(reloads()).toHaveLength(0);
    expect(refetches).toBe(0);
  });

  it('says the frozen applied sentence and refetches the detail on 200', async () => {
    setup({
      'POST /api/strategies/saw/reload': {
        session_id: 's1',
        from_hash: 'ccdd',
        to_hash: 'aabb',
        applies_at: 'next_closed_bar',
      },
    });
    m = await mount(<StrategyDriftPanel strategyId="saw" drift={DRIFTED} onRefetch={refetch} />);
    await click(buttonByText(m.container, 'Apply to session…'));
    await click(buttonByText(m.container, 'Apply to session'));
    expect(reloads()).toHaveLength(1);
    expect(m.container.textContent).toContain('Applied. The new parameters take effect at the next closed bar.');
    expect(refetches).toBe(1);
    expect(m.container.querySelector('dialog[open]')).toBeNull();
  });

  it('renders the 409 changed fields verbatim and applies nothing', async () => {
    setup({
      'POST /api/strategies/saw/reload': new ApiError(
        409,
        JSON.stringify({
          detail: {
            code: 'restart_required',
            message: 'restart required to change: risk, symbols',
            state: 'invalid',
            changed_fields: ['risk', 'symbols'],
          },
        }),
      ),
    });
    m = await mount(<StrategyDriftPanel strategyId="saw" drift={DRIFTED} onRefetch={refetch} />);
    await click(buttonByText(m.container, 'Apply to session…'));
    await click(buttonByText(m.container, 'Apply to session'));
    expect(reloads()).toHaveLength(1);
    const notice = m.container.querySelector('[role="status"]');
    expect(notice?.textContent).toBe('A restart is needed to apply: risk, symbols. Nothing was applied.');
    expect(notice?.className).toContain('text-danger');
  });

  it('says no running session on a 404 session_not_running, not the server message', async () => {
    setup({
      'POST /api/strategies/saw/reload': new ApiError(
        404,
        JSON.stringify({ detail: { code: 'session_not_running', message: 'No running session for saw', state: 'not_found' } }),
      ),
    });
    m = await mount(<StrategyDriftPanel strategyId="saw" drift={DRIFTED} onRefetch={refetch} />);
    await click(buttonByText(m.container, 'Apply to session…'));
    await click(buttonByText(m.container, 'Apply to session'));
    const notice = m.container.querySelector('[role="status"]');
    expect(notice?.textContent).toBe('No running session for this strategy.');
    expect(m.container.textContent).not.toContain('No running session for saw');
  });

  it('renders the server message verbatim when the strategy file is gone (404 strategy_not_found)', async () => {
    setup({
      'POST /api/strategies/saw/reload': new ApiError(
        404,
        JSON.stringify({ detail: { code: 'strategy_not_found', message: "Strategy 'saw' not found", state: 'not_found' } }),
      ),
    });
    m = await mount(<StrategyDriftPanel strategyId="saw" drift={DRIFTED} onRefetch={refetch} />);
    await click(buttonByText(m.container, 'Apply to session…'));
    await click(buttonByText(m.container, 'Apply to session'));
    const notice = m.container.querySelector('[role="status"]');
    expect(notice?.textContent).toBe("Strategy 'saw' not found");
    expect(notice?.className).toContain('text-danger');
    expect(m.container.textContent).not.toContain('No running session for this strategy.');
  });

  it('renders the 422 server message verbatim', async () => {
    setup({
      'POST /api/strategies/saw/reload': new ApiError(
        422,
        JSON.stringify({ detail: { code: 'invalid_config', message: 'Invalid strategy config: period must be >= 2', state: 'invalid' } }),
      ),
    });
    m = await mount(<StrategyDriftPanel strategyId="saw" drift={DRIFTED} onRefetch={refetch} />);
    await click(buttonByText(m.container, 'Apply to session…'));
    await click(buttonByText(m.container, 'Apply to session'));
    const notice = m.container.querySelector('[role="status"]');
    expect(notice?.textContent).toBe('Invalid strategy config: period must be >= 2');
    expect(notice?.className).toContain('text-danger');
  });
});
