import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, mount, type Mounted } from '@/test/pageHarness';
import type { BuilderState } from './types';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { SignalPreview } = await import('./SignalPreview');

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
  mlOverlay: null,
  editingId: null,
  strategyName: '',
  strategyDescription: '',
  exchangeId: 'kraken',
  symbol: 'ETHUSDT',
  leverage: 1,
  baseline: '',
};

let page: Mounted | null = null;

beforeEach(() => {
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('SignalPreview', () => {
  it('previews on the builder exchange/symbol and shows which bars were used', async () => {
    fetchApi.mockResolvedValue({
      signals: [{ timestamp: '2026-01-02T00:00:00+00:00', type: 'entry_long', price: 3100 }],
      metrics: { trades: 1, win_rate: 1, pnl: 12.5 },
      provenance: {
        exchange: 'kraken',
        symbol: 'ETHUSDT',
        timeframe: '4h',
        bar_count: 200,
        first_ts: '2025-12-01T00:00:00+00:00',
        last_ts: '2026-01-03T04:00:00+00:00',
        gap_count: 0,
        data_source: 'db',
      },
    });
    page = await mount(<SignalPreview state={STATE} />);

    await click(buttonByText(page.container, 'Preview Signals'));

    const [path, init] = fetchApi.mock.calls[0];
    expect(path).toBe('/api/strategies/preview');
    expect(JSON.parse(init.body)).toMatchObject({ exchange_id: 'kraken', symbol: 'ETHUSDT', timeframe: '4h' });
    expect(page.container.querySelector('[data-testid="preview-provenance"]')?.textContent).toBe(
      '200 stored bars, kraken ETHUSDT 4h, 2025-12-01 00:00 UTC to 2026-01-03 04:00 UTC',
    );
    expect(page.container.textContent).toContain('100.0%');
    expect(page.container.textContent).not.toContain('sample data');
  });

  it('shows the server message, not the raw JSON body, when no bars are stored', async () => {
    fetchApi.mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: {
            code: 'no_market_data',
            message: 'No market data for kraken ETHUSDT 4h; available: none stored',
            state: 'no_data',
          },
        }),
      ),
    );
    page = await mount(<SignalPreview state={STATE} />);

    await click(buttonByText(page.container, 'Preview Signals'));

    const text = page.container.textContent ?? '';
    expect(text).toContain('No market data for kraken ETHUSDT 4h; available: none stored');
    expect(text).not.toContain('"detail"');
  });

  it('renders PnL as signed USDT with no colour and an sr-only gain/loss label', async () => {
    fetchApi.mockResolvedValue({
      signals: [],
      metrics: { trades: 3, win_rate: 0.5, pnl: -12345.67 },
    });
    page = await mount(<SignalPreview state={STATE} />);

    await click(buttonByText(page.container, 'Preview Signals'));

    const text = page.container.textContent ?? '';
    expect(text).toContain('−12,345.67');
    expect(text).toContain('loss 12,345.67 USDT');
    expect(text).toContain('PnL (USDT)');
    const value = Array.from(page.container.querySelectorAll('p')).find((p) =>
      p.textContent?.includes('−12,345.67'),
    );
    expect(value).toBeDefined();
    expect(value?.className).not.toContain('text-danger');
    expect(page.container.querySelectorAll('.text-danger')).toHaveLength(0);
  });

  it('truncates the metric values so a large PnL cannot overflow its cell at 320px', async () => {
    fetchApi.mockResolvedValue({
      signals: [],
      metrics: { trades: 3, win_rate: 0.5, pnl: -12345.67 },
    });
    page = await mount(<SignalPreview state={STATE} />);

    await click(buttonByText(page.container, 'Preview Signals'));

    const cells = Array.from(page.container.querySelectorAll('p')).filter((p) =>
      p.className.includes('truncate'),
    );
    expect(cells).toHaveLength(3);
    for (const cell of cells) {
      const box = cell.parentElement;
      expect(box?.className).toContain('min-w-0');
    }
    expect(cells.some((c) => c.textContent?.includes('−12,345.67'))).toBe(true);
  });
});
