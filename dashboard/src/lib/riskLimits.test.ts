import { describe, expect, it } from 'vitest';
import {
  buildRiskLimitsUpdate,
  buildStrategyOverrideUpdate,
  effectiveOverride,
  fractionToPercentInput,
  hasOverride,
  limitChanges,
  limitText,
  riskLimitsToForm,
  strategyLimitText,
  strategyOverrideToForm,
} from './riskLimits';

const CFG = {
  max_position_pct: 0.1,
  max_risk_per_trade: 0.005,
  max_portfolio_heat: 0.125,
  max_daily_loss_pct: 0.0075,
  max_drawdown_pct: 0.15,
  max_concurrent_positions: 3,
  max_consecutive_losses: 5,
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
    ['max_consecutive_losses', '1.5'],
  ] as const)('rejects %s = %j', (field, value) => {
    const base = riskLimitsToForm(CFG);
    expect(buildRiskLimitsUpdate(base, { ...base, [field]: value })).toEqual({ ok: false, field });
  });
});

describe('limitText', () => {
  it('shows a fraction as its exact percent and a count as an integer', () => {
    expect(limitText('max_risk_per_trade', 0.005)).toBe('0.5%');
    expect(limitText('max_portfolio_heat', 0.125)).toBe('12.5%');
    expect(limitText('max_consecutive_losses', 5)).toBe('5');
  });
});

describe('limitChanges', () => {
  it('flags a higher ceiling as looser and a lower one as tighter, and skips unchanged fields', () => {
    expect(
      limitChanges(CFG, { max_drawdown_pct: 0.25, max_daily_loss_pct: 0.005, max_concurrent_positions: 3 }),
    ).toEqual([
      { field: 'max_daily_loss_pct', from: 0.0075, to: 0.005, looser: false },
      { field: 'max_drawdown_pct', from: 0.15, to: 0.25, looser: true },
    ]);
  });

  it('is empty for an empty update', () => {
    expect(limitChanges(CFG, {})).toEqual([]);
  });
});

describe('effectiveOverride', () => {
  it('takes the tighter of the stored override and the global, per cap', () => {
    expect(
      effectiveOverride(CFG, {
        max_position_pct: 0.05,
        max_risk_per_trade: 0.009,
        max_concurrent_positions: 2,
      }),
    ).toEqual({ max_position_pct: 0.05, max_risk_per_trade: 0.005, max_concurrent_positions: 2 });
  });

  it('falls back to the global for every cap left unset', () => {
    expect(effectiveOverride(CFG, { max_position_pct: null, max_risk_per_trade: null, max_concurrent_positions: null })).toEqual({
      max_position_pct: CFG.max_position_pct,
      max_risk_per_trade: CFG.max_risk_per_trade,
      max_concurrent_positions: CFG.max_concurrent_positions,
    });
  });
});

describe('buildStrategyOverrideUpdate', () => {
  const EMPTY = { max_position_pct: '', max_risk_per_trade: '', max_concurrent_positions: '' };

  it('turns empty fields into nulls (no override) and numbers into API units', () => {
    expect(buildStrategyOverrideUpdate({ ...EMPTY, max_position_pct: '5' }, CFG)).toEqual({
      ok: true,
      update: { max_position_pct: 0.05, max_risk_per_trade: null, max_concurrent_positions: null },
      clamped: [],
    });
    expect(buildStrategyOverrideUpdate({ ...EMPTY, max_concurrent_positions: '2' }, CFG)).toEqual({
      ok: true,
      update: { max_position_pct: null, max_risk_per_trade: null, max_concurrent_positions: 2 },
      clamped: [],
    });
  });

  it('clamps a looser entry to the global value and names the field', () => {
    expect(buildStrategyOverrideUpdate({ ...EMPTY, max_position_pct: '50' }, CFG)).toEqual({
      ok: true,
      update: { max_position_pct: CFG.max_position_pct, max_risk_per_trade: null, max_concurrent_positions: null },
      clamped: ['max_position_pct'],
    });
    expect(buildStrategyOverrideUpdate({ ...EMPTY, max_concurrent_positions: '9' }, CFG)).toEqual({
      ok: true,
      update: { max_position_pct: null, max_risk_per_trade: null, max_concurrent_positions: CFG.max_concurrent_positions },
      clamped: ['max_concurrent_positions'],
    });
  });

  it.each([
    ['max_position_pct', 'abc'],
    ['max_concurrent_positions', '2.5'],
  ])('refuses a non-number in %s', (field, value) => {
    expect(buildStrategyOverrideUpdate({ ...EMPTY, [field]: value }, CFG)).toEqual({ ok: false, field });
  });
});

describe('strategy override text', () => {
  it('shows an unset cap as a dash and a stored one exactly', () => {
    expect(strategyLimitText('max_position_pct', null)).toBe('—');
    expect(strategyLimitText('max_position_pct', 0.05)).toBe('5%');
    expect(strategyLimitText('max_concurrent_positions', 2)).toBe('2');
    expect(hasOverride({ max_position_pct: null, max_risk_per_trade: null, max_concurrent_positions: null })).toBe(false);
    expect(hasOverride({ max_position_pct: 0.05, max_risk_per_trade: null, max_concurrent_positions: null })).toBe(true);
  });

  it('round-trips a stored override through the form', () => {
    const over = { max_position_pct: 0.05, max_risk_per_trade: null, max_concurrent_positions: 2 };
    const form = strategyOverrideToForm(over);
    expect(form).toEqual({ max_position_pct: '5', max_risk_per_trade: '', max_concurrent_positions: '2' });
    expect(buildStrategyOverrideUpdate(form, CFG)).toEqual({ ok: true, update: over, clamped: [] });
  });
});
