import { afterEach, describe, expect, it } from 'vitest';
import { mount, type Mounted } from '@/test/pageHarness';
import { perpPair, perpTrailingStop } from '@/test/fixtures';
import type { OpenOrder } from '@/lib/derive';

const { PerpProtection, perpProtectiveUnits } = await import('./PerpProtection');

let m: Mounted | null = null;
afterEach(() => {
  m?.unmount();
  m = null;
});

const text = () => m?.container.textContent ?? '';

describe('perpProtectiveUnits (P7-G: the pair as ONE unit)', () => {
  it('groups the two legs sharing an oco_id into one pair carrying both prices', () => {
    const units = perpProtectiveUnits(perpPair());
    expect(units).toHaveLength(1);
    expect(units[0]).toMatchObject({
      kind: 'pair',
      id: 'p7g-pair-1',
      symbol: 'BTCUSDT',
      side: 'SELL',
      quantity: 0.002,
      takeProfit: 71000,
      stop: 64000,
      reduceOnly: true,
    });
  });

  it('keeps a resting non-protective limit out and renders a lone pair leg as itself', () => {
    const limit: OpenOrder = {
      id: 'lim-1',
      symbol: 'BTCUSDT',
      side: 'BUY',
      type: 'LIMIT',
      amount: 0.002,
      price: 60000,
      stop_price: null,
      reason: 'limit',
      protective: false,
    };
    const loneTakeProfit = [{ ...perpPair({ oco_id: 'p7g-pair-2' })[0], id: 'p7g-tp-lone' }];
    const units = perpProtectiveUnits([...perpPair(), ...loneTakeProfit, limit]);
    expect(units).toHaveLength(2);
    expect(units[0].kind).toBe('pair');
    expect(units[1]).toMatchObject({ kind: 'single', id: 'p7g-tp-lone', reason: 'take_profit', stop: 71000 });
  });

  it('carries a trailing group as one unit with its fraction and trigger', () => {
    const units = perpProtectiveUnits([perpTrailingStop()]);
    expect(units).toHaveLength(1);
    expect(units[0]).toMatchObject({ kind: 'single', reason: 'trailing_stop', trailPct: 0.02, stop: 64800 });
  });
});

describe('PerpProtection (the two-leg card)', () => {
  it('renders the pair as one named unit with both prices, not two stop rows', async () => {
    m = await mount(<PerpProtection orders={perpPair()} />);
    expect(text()).toContain('Protective pair — one two-leg unit, no OCO on futures');
    expect(text()).toContain('Take-profit 71,000.00 · Stop 64,000.00 · 0.00200 BTC (SELL)');
    expect(text()).toContain('reduce-only — one leg fills and the other cancels');
    expect((text().match(/Protective pair/g) ?? []).length).toBe(1);
  });

  it('renders the trail note from the payload fraction and a stop-only group as one line', async () => {
    m = await mount(<PerpProtection orders={[perpTrailingStop(), perpPair()[1]]} />);
    expect(text()).toContain('Trailing stop 2% · stop at 64,800.00 · 0.00200 BTC (SELL)');
    expect(text()).toContain('Stop at 64,000.00 · 0.00200 BTC (SELL)');
  });

  it('renders nothing with no resting protective orders', async () => {
    m = await mount(<PerpProtection orders={[]} />);
    expect(text()).toBe('');
  });
});
