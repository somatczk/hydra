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
  baseline: '',
};

let m: Mounted | null = null;
let saved: Array<[string, string]>;
let closes: number;

function render(props: { editingId?: string | null; initialName?: string } = {}) {
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
      initialExchangeId="binance"
      initialSymbol="BTCUSDT"
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
