import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
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
import type { BuilderState } from './types';
import type { UniverseSnapshot } from './UniversePicker';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { StrategyNameDialog } = await import('./StrategyNameDialog');

/** A native select change, the React-controlled way. */
async function pickByLabel(container: HTMLElement, label: string, value: string): Promise<void> {
  const el = Array.from(container.querySelectorAll('label')).find((l) => l.textContent === label);
  if (!el) throw new Error(`no label ${label}`);
  const select = document.getElementById(el.htmlFor);
  if (!(select instanceof HTMLSelectElement)) throw new Error(`no select for ${label}`);
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
  await act(async () => {
    setter?.call(select, value);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

function selectByLabel(container: HTMLElement, label: string): HTMLSelectElement {
  const el = Array.from(container.querySelectorAll('label')).find((l) => l.textContent === label);
  if (!el) throw new Error(`no label ${label}`);
  const select = document.getElementById(el.htmlFor);
  if (!(select instanceof HTMLSelectElement)) throw new Error(`no select for ${label}`);
  return select;
}

const STATE: BuilderState = {
  entryLong: {
    operator: 'AND',
    conditions: [{ id: 'c1', indicator: 'rsi', params: { period: 14 }, comparator: 'crosses_above', value: 30 }],
  },
  exitLong: { operator: 'AND', conditions: [] },
  entryShort: { operator: 'AND', conditions: [] },
  exitShort: { operator: 'AND', conditions: [] },
  timeframes: { primary: '4h' },
  risk: {
    stopLoss: { method: 'atr', value: 2 },
    takeProfit: { method: 'atr', value: 3 },
    sizing: { method: 'fixed_fractional', riskPerTradePct: 1, maxPositionPct: 10 },
  },
  mlOverlay: { modelName: 'm1', confidenceThreshold: 0.6 },
  editingId: null,
  strategyName: '',
  strategyDescription: '',
  exchangeId: 'binance',
  symbol: 'BTCUSDT',
  leverage: 1,
  schedule: null,
  tunables: {},
  baseline: '',
};

/** The stored top-100 ranking exactly as GET /api/market/universe serves it. */
const UNIVERSE: UniverseSnapshot = {
  exchange_id: 'binance',
  quote_asset: 'USDT',
  source: 'binance spot rolling-24h quote volume (GET /api/v3/ticker/24hr)',
  ranked_at: '2026-10-29T08:00:00+00:00',
  age_hours: 2,
  count: 3,
  symbols: [
    { rank: 1, symbol: 'BTCUSDT', quote_volume: 2_000_000_000 },
    { rank: 2, symbol: 'ETHUSDT', quote_volume: 800_000_000 },
    { rank: 3, symbol: 'SOLUSDT', quote_volume: 300_000_000 },
  ],
};

/** The picker's own compact format, so assertions never pin an ICU string. */
const VOLUME = new Intl.NumberFormat('en-GB', { notation: 'compact', maximumFractionDigits: 1 });

/** The exact 422 the write gate answers (`services/universe.py::validate_symbol`). */
function gateRefusal(symbol: string): ApiError {
  const message =
    `${symbol} is not in the current top-100 universe (ranked at ${UNIVERSE.ranked_at}); ` +
    'running sessions and stored strategies are unaffected';
  return new ApiError(
    422,
    JSON.stringify({
      detail: {
        code: 'symbol_not_in_universe',
        message,
        state: 'invalid',
        symbol,
        universe_ranked_at: UNIVERSE.ranked_at,
        universe_count: UNIVERSE.count,
      },
    }),
  );
}

let m: Mounted | null = null;
let saved: Array<[string, string]>;
let closes: number;
let calls: Array<{ path: string; init?: RequestInit }>;

interface RenderProps {
  editingId?: string | null;
  initialName?: string;
  initialSymbol?: string;
  initialExchangeId?: string;
  initialLeverage?: number;
  state?: BuilderState;
  /** Defaults to the member ranking; pass an Error for the unreadable universe. */
  universe?: unknown;
  mutations?: Mutations;
}

function render(props: RenderProps = {}): Promise<Mounted> {
  const routes: Routes = { '/api/market/universe': props.universe ?? UNIVERSE };
  const mutations: Mutations = props.mutations ?? {
    'POST /api/strategies/save': { id: 'strat-7' },
    'PUT /api/strategies/strat-9': { id: 'ignored' },
  };
  fetchApi.mockImplementation(routeFetch(routes, calls, mutations));
  return mount(
    <StrategyNameDialog
      open
      onClose={() => {
        closes += 1;
      }}
      state={props.state ?? STATE}
      onSaved={(name, id) => saved.push([name, id])}
      editingId={props.editingId ?? null}
      initialName={props.initialName ?? ''}
      initialDescription=""
      initialExchangeId={props.initialExchangeId ?? 'binance'}
      initialSymbol={props.initialSymbol ?? 'BTCUSDT'}
      initialLeverage={props.initialLeverage ?? 1}
    />,
  );
}

const dialog = () => m!.container.querySelector('dialog') as HTMLDialogElement;
const symbolInput = () => inputByLabel(dialog(), 'Symbol');

/** jsdom does not move focus on click; focus it the way the browser would on interaction. */
async function focusInput(input: HTMLInputElement): Promise<void> {
  await act(async () => {
    input.focus();
  });
  await flush();
}

/** A keydown on the symbol combobox the way the browser delivers it. */
async function press(input: HTMLInputElement, key: string): Promise<void> {
  await act(async () => {
    input.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
  });
  await flush();
}

beforeEach(() => {
  dialogPolyfill();
  fetchApi.mockReset();
  saved = [];
  closes = 0;
  calls = [];
});

afterEach(() => {
  m?.unmount();
  m = null;
});

describe('StrategyNameDialog', () => {
  it('is a native dialog that focuses the name field and keeps the primary off until a name is typed', async () => {
    m = await render();

    expect(dialog().open).toBe(true);
    expect(dialog().tagName).toBe('DIALOG');
    const name = inputByLabel(dialog(), 'Strategy name');
    expect(document.activeElement).toBe(name);
    expect(buttonByText(dialog(), 'Save strategy').disabled).toBe(true);

    await typeInto(name, 'RSI bot');
    expect(buttonByText(dialog(), 'Save strategy').disabled).toBe(false);
  });

  it('posts the exact save body for a new strategy and reports the saved id', async () => {
    m = await render({ initialName: 'RSI bot' });

    await click(buttonByText(dialog(), 'Save strategy'));

    const post = calls.find((c) => c.init?.method === 'POST');
    expect(post?.path).toBe('/api/strategies/save');
    expect(JSON.parse(String(post?.init?.body))).toEqual({
      name: 'RSI bot',
      description: '',
      exchange_id: 'binance',
      symbol: 'BTCUSDT',
      rules: {
        entry_long: {
          operator: 'AND',
          conditions: [{ indicator: 'rsi', params: { period: 14 }, comparator: 'crosses_above', value: 30 }],
        },
        exit_long: null,
        entry_short: null,
        exit_short: null,
      },
      timeframes: { primary: '4h' },
      risk: {
        stop_loss_method: 'atr',
        stop_loss_value: 2,
        take_profit_method: 'atr',
        take_profit_value: 3,
        sizing_method: 'fixed_fractional',
        sizing_params: { risk_per_trade_pct: 1, max_position_pct: 10 },
      },
      enable_immediately: false,
      ml_overlay: { model_name: 'm1', confidence_threshold: 0.6 },
      leverage: 1,
      schedule: {},
      parameters: {},
    });
    expect(saved).toEqual([['RSI bot', 'strat-7']]);
    expect(closes).toBe(1);
  });

  it('puts to the strategy id when editing', async () => {
    m = await render({ editingId: 'strat-9', initialName: 'Saw' });

    expect(dialog().textContent).toContain('Update strategy');

    await click(buttonByText(dialog(), 'Save strategy'));

    const put = calls.find((c) => c.init?.method === 'PUT');
    expect(put?.path).toBe('/api/strategies/strat-9');
    expect(saved).toEqual([['Saw', 'strat-9']]);
  });

  it('puts the builder\'s schedule as sorted UTC hours and Monday=0 days', async () => {
    const state: BuilderState = { ...STATE, timeframes: { primary: '1h' }, schedule: { hours: [14, 13], days: [4, 0] } };
    m = await render({ editingId: 'strat-9', initialName: 'Saw', state });

    await click(buttonByText(dialog(), 'Save strategy'));

    const put = calls.find((c) => c.init?.method === 'PUT');
    expect(JSON.parse(String(put?.init?.body)).schedule).toEqual({ active_hours_utc: [13, 14], active_days: [0, 4] });
  });

  it('blocks a schedule no primary bar can match (the engine 422s it) and says why', async () => {
    const state: BuilderState = { ...STATE, schedule: { hours: [13, 14], days: [0] } };
    m = await render({ editingId: 'strat-9', initialName: 'Saw', state });

    expect(buttonByText(dialog(), 'Save strategy').disabled).toBe(true);
    expect(dialog().querySelector('[role="alert"]')?.textContent).toBe(
      'Trading schedule: 4h bars open only at 0, 4, 8, 12, 16, 20 UTC, so no entry could ever pass: include one of those hours.',
    );
    expect(calls.some((c) => c.init?.method === 'PUT')).toBe(false);
  });

  it('closes on Esc when not saving, but not while the save is in flight', async () => {
    let resolvePost: (v: unknown) => void = () => {};
    const pending = new Promise((r) => {
      resolvePost = r;
    });
    m = await render({
      initialName: 'RSI bot',
      mutations: { 'POST /api/strategies/save': () => pending },
    });

    await act(async () => {
      dialog().dispatchEvent(new Event('cancel', { cancelable: true }));
    });
    expect(closes).toBe(1);

    await click(buttonByText(dialog(), 'Save strategy'));
    expect(dialog().textContent).toContain('Saving…');
    expect(dialog().textContent).toContain("Closing this won't cancel the save.");
    await act(async () => {
      dialog().dispatchEvent(new Event('cancel', { cancelable: true }));
    });
    expect(closes).toBe(1);

    await act(async () => resolvePost({ id: 'strat-7' }));
    await flush();
    expect(closes).toBe(2);
    expect(saved).toEqual([['RSI bot', 'strat-7']]);
  });

  it('stays open with the server message when the save fails', async () => {
    m = await render({
      initialName: 'RSI bot',
      mutations: {
        'POST /api/strategies/save': new ApiError(
          422,
          JSON.stringify({
            detail: { code: 'invalid_config', message: 'Rules reference unknown indicator', state: 'invalid' },
          }),
        ),
      },
    });

    await click(buttonByText(dialog(), 'Save strategy'));

    expect(closes).toBe(0);
    expect(saved).toHaveLength(0);
    expect(dialog().textContent).toContain('Rules reference unknown indicator');
    expect(dialog().textContent).not.toContain('"detail"');
  });
});

describe('StrategyNameDialog perps fields (P4-F: venue / market_type / leverage / isolated)', () => {
  it('offers the binanceusdm venue and shows the perps region only for it', async () => {
    m = await render({ initialName: 'Perp bot' });

    const dialog = () => m!.container.querySelector('dialog') as HTMLDialogElement;
    expect(dialog().textContent).not.toContain('Perps (Binance USD⌂-M)');
    expect(Array.from(dialog().querySelectorAll('label')).map((l) => l.textContent)).not.toContain('Leverage (1–3×)');

    await pickByLabel(dialog(), 'Exchange', 'binanceusdm');
    expect(dialog().textContent).toContain('Perps (Binance USD⌂-M)');
    expect(dialog().textContent).toContain('Market type: FUTURES · Margin mode: isolated (the only mode in 2.8.0).');
    expect(dialog().textContent).toContain('The account-wide cap (3×) is enforced again at session start; spot strategies stay 1×.');
  });

  it('sends the picked leverage for a perps venue and 1× for spot', async () => {
    m = await render({ initialName: 'Perp bot' });
    const dialog = () => m!.container.querySelector('dialog') as HTMLDialogElement;

    await pickByLabel(dialog(), 'Exchange', 'binanceusdm');
    await pickByLabel(dialog(), 'Leverage (1–3×)', '2');
    await click(buttonByText(dialog(), 'Save strategy'));
    expect(JSON.parse(String(calls.filter((c) => c.init?.method === 'POST').at(-1)?.init?.body))).toMatchObject({ exchange_id: 'binanceusdm', leverage: 2 });

    m?.unmount();
    m = await render({ initialName: 'Spot bot' });
    await click(buttonByText(dialog(), 'Save strategy'));
    expect(JSON.parse(String(calls.filter((c) => c.init?.method === 'POST').at(-1)?.init?.body))).toMatchObject({ exchange_id: 'binance', leverage: 1 });
  });

  it('round-trips a stored perp strategy\u2019s leverage into the select (never silently downgrades it)', async () => {
    m = await render({ editingId: 'strat-9', initialName: 'Saw', initialExchangeId: 'binanceusdm', initialLeverage: 3 });
    const dialog = () => m!.container.querySelector('dialog') as HTMLDialogElement;

    const sel = selectByLabel(dialog(), 'Leverage (1–3×)');
    expect(sel.value).toBe('3');
    await click(buttonByText(dialog(), 'Save strategy'));
    const put = calls.find((c) => c.init?.method === 'PUT');
    expect(put?.path).toBe('/api/strategies/strat-9');
    expect(JSON.parse(String(put?.init?.body))).toMatchObject({ exchange_id: 'binanceusdm', leverage: 3 });
  });

  it('drops the leverage to 1× when the venue moves back to spot (the venue-pair rule, client-side)', async () => {
    m = await render({ initialName: 'Perp bot', initialExchangeId: 'binanceusdm', initialLeverage: 2 });
    const dialog = () => m!.container.querySelector('dialog') as HTMLDialogElement;

    await pickByLabel(dialog(), 'Exchange', 'binance');
    expect(dialog().textContent).not.toContain('Perps (Binance USD⌂-M)');
    await click(buttonByText(dialog(), 'Save strategy'));
    expect(JSON.parse(String(calls.filter((c) => c.init?.method === 'POST').at(-1)?.init?.body))).toMatchObject({ exchange_id: 'binance', leverage: 1 });
  });

  it('surfaces the save-path validation refusals verbatim: the pydantic bounds and the venue-pair rule', async () => {
    m = await render({
      initialName: 'Perp bot',
      initialExchangeId: 'binanceusdm',
      initialLeverage: 2,
      mutations: {
        'POST /api/strategies/save': new ApiError(
          422,
          JSON.stringify({
            detail: {
              code: 'value_error',
              message: 'Input should be less than or equal to 3',
              state: 'invalid',
            },
          }),
        ),
      },
    });
    await click(buttonByText(dialog(), 'Save strategy'));
    expect(dialog().textContent).toContain('Input should be less than or equal to 3');

    m?.unmount();
    m = await render({
      initialName: 'Spot bot',
      mutations: {
        'POST /api/strategies/save': new ApiError(
          422,
          JSON.stringify({
            detail: {
              code: 'value_error',
              message: 'Leverage applies to binanceusdm (futures) strategies only, got 2x',
              state: 'invalid',
            },
          }),
        ),
      },
    });
    await click(buttonByText(dialog(), 'Save strategy'));
    expect(dialog().textContent).toContain('Leverage applies to binanceusdm (futures) strategies only, got 2x');
    expect(closes).toBe(0);
  });
});


describe('StrategyNameDialog symbol picker (P6-UI)', () => {
  it('lists the universe members with rank and 24h volume and commits a picked member', async () => {
    m = await render({ initialName: 'RSI bot' });
    await flush();

    const input = symbolInput();
    expect(input.getAttribute('role')).toBe('combobox');
    expect(input.getAttribute('aria-expanded')).toBe('false');
    expect(dialog().textContent).toContain('Top 3 binance USDT pairs by 24h quote volume');

    await focusInput(input);
    expect(input.getAttribute('aria-expanded')).toBe('true');
    const listbox = dialog().querySelector('[role="listbox"]');
    expect(listbox).not.toBeNull();
    const options = Array.from(listbox!.querySelectorAll('[role="option"]'));
    expect(options.map((o) => o.textContent)).toEqual([
      `#1BTCUSDT24h ${VOLUME.format(2_000_000_000)}`,
      `#2ETHUSDT24h ${VOLUME.format(800_000_000)}`,
      `#3SOLUSDT24h ${VOLUME.format(300_000_000)}`,
    ]);
    expect(options[0].id).toBe(input.getAttribute('aria-activedescendant'));

    await typeInto(input, 'SOL');
    await press(input, 'Enter');

    expect(input.getAttribute('aria-expanded')).toBe('false');
    expect(input.value).toBe('SOLUSDT');

    await click(buttonByText(dialog(), 'Save strategy'));
    const post = calls.find((c) => c.init?.method === 'POST');
    expect(JSON.parse(String(post?.init?.body)).symbol).toBe('SOLUSDT');
    expect(saved).toEqual([['RSI bot', 'strat-7']]);
  });

  it('blocks a non-member draft: no option to pick, an inline reason, the save off', async () => {
    m = await render({ initialName: 'RSI bot' });
    await flush();

    const input = symbolInput();
    await typeInto(input, 'FOO');
    await flush();

    expect(dialog().querySelectorAll('[role="option"]')).toHaveLength(0);
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(dialog().textContent).toContain('FOO is not in the current top-100 universe — pick a member from the list.');
    expect(buttonByText(dialog(), 'Save strategy').disabled).toBe(true);
    expect(fetchApi.mock.calls.some(([path]) => path === '/api/strategies/save')).toBe(false);
  });

  it('accepts the slash form the backend normalizes (BTC/USDT is the member BTCUSDT)', async () => {
    m = await render({ initialName: 'RSI bot' });
    await flush();

    const input = symbolInput();
    await typeInto(input, 'BTC/USDT');
    await flush();

    expect(input.getAttribute('aria-invalid')).not.toBe('true');
    expect(buttonByText(dialog(), 'Save strategy').disabled).toBe(false);
  });

  it('judges the trimmed draft the request sends: a padded member commits, a padded non-member still blocks', async () => {
    m = await render({ initialName: 'RSI bot', initialSymbol: ' BTCUSDT' });
    await flush();

    expect(buttonByText(dialog(), 'Save strategy').disabled).toBe(false);
    await click(buttonByText(dialog(), 'Save strategy'));
    const post = calls.find((c) => c.init?.method === 'POST');
    expect(JSON.parse(String(post?.init?.body)).symbol).toBe('BTCUSDT');
    expect(saved).toEqual([['RSI bot', 'strat-7']]);

    m?.unmount();
    const savesBefore = calls.filter((c) => c.path === '/api/strategies/save').length;
    m = await render({ initialName: 'RSI bot', initialSymbol: ' FOOBAR' });
    await flush();
    expect(buttonByText(dialog(), 'Save strategy').disabled).toBe(true);
    expect(calls.filter((c) => c.path === '/api/strategies/save')).toHaveLength(savesBefore);
  });

  it('renders the engine 422 guided when a call bypasses the picker: the backend copy verbatim plus the pointer', async () => {
    // first, the picker gate is ahead of the engine: a non-member draft cannot even be sent
    m = await render({ initialName: 'RSI bot', initialSymbol: 'FOOBAR' });
    await flush();
    expect(buttonByText(dialog(), 'Save strategy').disabled).toBe(true);
    expect(fetchApi.mock.calls.some(([path]) => path === '/api/strategies/save')).toBe(false);
    m?.unmount();

    // but with the universe unreadable the dialog can't pre-validate: the save reaches the
    // engine, whose 422 renders inline, guided — never a generic "Save failed"
    m = await render({
      initialName: 'RSI bot',
      initialSymbol: 'FOOBAR',
      universe: unavailable('The top-100 universe has not been computed yet'),
      mutations: { 'POST /api/strategies/save': gateRefusal('FOOBAR') },
    });
    await flush();
    expect(dialog().textContent).toContain('Universe unavailable');
    expect(buttonByText(dialog(), 'Save strategy').disabled).toBe(false);

    await click(buttonByText(dialog(), 'Save strategy'));

    expect(closes).toBe(0);
    expect(saved).toHaveLength(0);
    const gateCard = dialog().querySelector('[data-testid="universe-gate-error"]');
    expect(gateCard).not.toBeNull();
    expect(gateCard?.getAttribute('role')).toBe('alert');
    expect(gateCard?.textContent).toContain(
      'FOOBAR is not in the current top-100 universe (ranked at 2026-10-29T08:00:00+00:00); running sessions and stored strategies are unaffected',
    );
    expect(gateCard?.textContent).toContain(
      'Pick a member of the current top-100 universe from the symbol field above',
    );
    expect(gateCard?.textContent).not.toContain('Save failed');
  });
});
