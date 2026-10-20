import { describe, expect, it } from 'vitest';
import { buildRiskLimitsUpdate, fractionToPercentInput, riskLimitsToForm } from './riskLimits';

const CFG = {
  max_position_pct: 0.1,
  max_risk_per_trade: 0.005,
  max_portfolio_heat: 0.125,
  max_daily_loss_pct: 0.0075,
  max_drawdown_pct: 0.15,
  max_concurrent_positions: 3,
};

describe('fractionToPercentInput', () => {
  it.each([
    [0.005, '0.5'],
    [0.0075, '0.75'],
    [0.125, '12.5'],
    [0.1, '10'],
    [0.07, '7'],
    [0.00125, '0.125'],
    [0, '0'],
  ])('%s -> %s', (fraction, text) => {
    expect(fractionToPercentInput(fraction)).toBe(text);
  });
});

describe('buildRiskLimitsUpdate', () => {
  it('sends nothing when nothing changed, so sub-percent limits are not rewritten', () => {
    const form = riskLimitsToForm(CFG);
    expect(buildRiskLimitsUpdate(form, { ...form })).toEqual({ ok: true, update: {} });
  });

  it('sends only the changed field, converted to a fraction', () => {
    const base = riskLimitsToForm(CFG);
    expect(buildRiskLimitsUpdate(base, { ...base, max_drawdown_pct: '20' })).toEqual({
      ok: true,
      update: { max_drawdown_pct: 0.2 },
    });
    expect(buildRiskLimitsUpdate(base, { ...base, max_risk_per_trade: '0.25' })).toEqual({
      ok: true,
      update: { max_risk_per_trade: 0.0025 },
    });
    expect(buildRiskLimitsUpdate(base, { ...base, max_concurrent_positions: '7' })).toEqual({
      ok: true,
      update: { max_concurrent_positions: 7 },
    });
  });

  it('converts every loaded value back to the exact fraction', () => {
    const blank = riskLimitsToForm({ ...CFG, max_concurrent_positions: 0 });
    for (const k of Object.keys(blank) as Array<keyof typeof blank>) blank[k] = '';
    expect(buildRiskLimitsUpdate(blank, riskLimitsToForm(CFG))).toEqual({ ok: true, update: CFG });
  });

  it.each([
    ['max_drawdown_pct', ''],
    ['max_drawdown_pct', 'abc'],
    ['max_concurrent_positions', '2.5'],
  ] as const)('rejects %s = %j', (field, value) => {
    const base = riskLimitsToForm(CFG);
    expect(buildRiskLimitsUpdate(base, { ...base, [field]: value })).toEqual({ ok: false, field });
  });
});
