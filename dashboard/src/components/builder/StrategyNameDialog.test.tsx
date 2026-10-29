import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, dialogPolyfill, flush, inputByLabel, mount, typeInto, type Mounted } from '@/test/pageHarness';
import type { BuilderState } from './types';

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
  baseline: '',
};

let m: Mounted | null = null;
let saved: Array<[string, string]>;
let closes: number;

function render(props: { editingId?: string | null; initialName?: string; initialExchangeId?: string; initialLeverage?: number } = {}) {
  return mount(
    <StrategyNameDialog
      open
      onClose={() => {
        closes += 1;
      }}
      state={STATE}
      onSaved={(name, id) => saved.push([name, id])}
      editingId={props.editingId ?? null}
      initialName={props.initialName ?? ''}
      initialDescription=""
      initialExchangeId={props.initialExchangeId ?? 'binance'}
      initialSymbol="BTCUSDT"
      initialLeverage={props.initialLeverage ?? 1}
    />,
  );
}

const dialog = () => m!.container.querySelector('dialog') as HTMLDialogElement;

beforeEach(() => {
  dialogPolyfill();
  fetchApi.mockReset();
  saved = [];
  closes = 0;
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
    fetchApi.mockResolvedValue({ id: 'strat-7' });
    m = await render({ initialName: 'RSI bot' });

    await click(buttonByText(dialog(), 'Save strategy'));

    const [path, init] = fetchApi.mock.calls[0];
    expect(path).toBe('/api/strategies/save');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({
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
    });
    expect(saved).toEqual([['RSI bot', 'strat-7']]);
    expect(closes).toBe(1);
  });

  it('puts to the strategy id when editing', async () => {
    fetchApi.mockResolvedValue({ id: 'ignored' });
    m = await render({ editingId: 'strat-9', initialName: 'Saw' });

    expect(dialog().textContent).toContain('Update strategy');

    await click(buttonByText(dialog(), 'Save strategy'));

    const [path, init] = fetchApi.mock.calls[0];
    expect(path).toBe('/api/strategies/strat-9');
    expect(init?.method).toBe('PUT');
    expect(saved).toEqual([['Saw', 'strat-9']]);
  });

  it('closes on Esc when not saving, but not while the save is in flight', async () => {
    let resolvePost: (v: unknown) => void = () => {};
    fetchApi.mockImplementation(
      () =>
        new Promise((r) => {
          resolvePost = r;
        }),
    );
    m = await render({ initialName: 'RSI bot' });

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
    fetchApi.mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: { code: 'invalid_config', message: 'Rules reference unknown indicator', state: 'invalid' },
        }),
      ),
    );
    m = await render({ initialName: 'RSI bot' });

    await click(buttonByText(dialog(), 'Save strategy'));

    expect(closes).toBe(0);
    expect(saved).toHaveLength(0);
    expect(dialog().textContent).toContain('Rules reference unknown indicator');
    expect(dialog().textContent).not.toContain('"detail"');
  });
});

describe('StrategyNameDialog perps fields (P4-F: venue / market_type / leverage / isolated)', () => {
  it('offers the binanceusdm venue and shows the perps region only for it', async () => {
    fetchApi.mockResolvedValue({ id: 'strat-7' });
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
    fetchApi.mockResolvedValue({ id: 'strat-7' });
    m = await render({ initialName: 'Perp bot' });
    const dialog = () => m!.container.querySelector('dialog') as HTMLDialogElement;

    await pickByLabel(dialog(), 'Exchange', 'binanceusdm');
    await pickByLabel(dialog(), 'Leverage (1–3×)', '2');
    await click(buttonByText(dialog(), 'Save strategy'));
    expect(JSON.parse(String(fetchApi.mock.calls[0][1]?.body))).toMatchObject({ exchange_id: 'binanceusdm', leverage: 2 });

    fetchApi.mockClear();
    m?.unmount();
    m = await render({ initialName: 'Spot bot' });
    await click(buttonByText(dialog(), 'Save strategy'));
    expect(JSON.parse(String(fetchApi.mock.calls[0][1]?.body))).toMatchObject({ exchange_id: 'binance', leverage: 1 });
  });

  it('round-trips a stored perp strategy\u2019s leverage into the select (never silently downgrades it)', async () => {
    fetchApi.mockResolvedValue({ id: 'strat-9' });
    m = await render({ editingId: 'strat-9', initialName: 'Saw', initialExchangeId: 'binanceusdm', initialLeverage: 3 });
    const dialog = () => m!.container.querySelector('dialog') as HTMLDialogElement;

    const sel = selectByLabel(dialog(), 'Leverage (1–3×)');
    expect(sel.value).toBe('3');
    await click(buttonByText(dialog(), 'Save strategy'));
    const [path, init] = fetchApi.mock.calls[0];
    expect(path).toBe('/api/strategies/strat-9');
    expect(JSON.parse(String(init?.body))).toMatchObject({ exchange_id: 'binanceusdm', leverage: 3 });
  });

  it('drops the leverage to 1× when the venue moves back to spot (the venue-pair rule, client-side)', async () => {
    fetchApi.mockResolvedValue({ id: 'strat-7' });
    m = await render({ initialName: 'Perp bot', initialExchangeId: 'binanceusdm', initialLeverage: 2 });
    const dialog = () => m!.container.querySelector('dialog') as HTMLDialogElement;

    await pickByLabel(dialog(), 'Exchange', 'binance');
    expect(dialog().textContent).not.toContain('Perps (Binance USD⌂-M)');
    await click(buttonByText(dialog(), 'Save strategy'));
    expect(JSON.parse(String(fetchApi.mock.calls[0][1]?.body))).toMatchObject({ exchange_id: 'binance', leverage: 1 });
  });

  it('surfaces the save-path validation refusals verbatim: the pydantic bounds and the venue-pair rule', async () => {
    fetchApi.mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: {
            code: 'value_error',
            message: 'Input should be less than or equal to 3',
            state: 'invalid',
          },
        }),
      ),
    );
    m = await render({ initialName: 'Perp bot', initialExchangeId: 'binanceusdm', initialLeverage: 2 });
    await click(buttonByText(dialog(), 'Save strategy'));
    expect(dialog().textContent).toContain('Input should be less than or equal to 3');

    fetchApi.mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: {
            code: 'value_error',
            message: 'Leverage applies to binanceusdm (futures) strategies only, got 2x',
            state: 'invalid',
          },
        }),
      ),
    );
    m?.unmount();
    m = await render({ initialName: 'Spot bot' });
    await click(buttonByText(dialog(), 'Save strategy'));
    expect(dialog().textContent).toContain('Leverage applies to binanceusdm (futures) strategies only, got 2x');
    expect(closes).toBe(0);
  });
});
