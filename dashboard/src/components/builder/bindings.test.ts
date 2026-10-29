import { describe, expect, it } from 'vitest';
import { bindingsOf, bindingsText, dropBindings } from './bindings';

describe('condition bindings', () => {
  it('keeps every stored binding, the {} opt-out included, and treats the API nulls as absent', () => {
    expect(
      bindingsOf({ param_key: null, param_overrides: {}, value_param_key: 'rsi_lower', value_ref_overrides: null }),
    ).toEqual({ param_overrides: {}, value_param_key: 'rsi_lower' });
    expect(bindingsOf({ param_key: null, param_overrides: null, value_param_key: null, value_ref_overrides: null })).toBeUndefined();
  });

  it('drops only the side an edit invalidates and names what stops binding', () => {
    const b = {
      param_key: 'rsi_period',
      param_overrides: { rsi_len: 'period' },
      value_param_key: 'rsi_lower',
      value_ref_overrides: { slow: 'period' },
    };
    expect(dropBindings(b, 'indicator')).toEqual({
      kept: { value_param_key: 'rsi_lower', value_ref_overrides: { slow: 'period' } },
      dropped: ['rsi_period', 'rsi_len'],
    });
    expect(dropBindings(b, 'value')).toEqual({
      kept: { param_key: 'rsi_period', param_overrides: { rsi_len: 'period' } },
      dropped: ['rsi_lower', 'slow'],
    });
    // the explicit opt-out is not a binding: an indicator change keeps it
    expect(dropBindings({ param_overrides: {} }, 'indicator')).toEqual({ kept: { param_overrides: {} }, dropped: [] });
    expect(dropBindings(undefined, 'value')).toEqual({ kept: undefined, dropped: [] });
  });

  it('names every binding in one line', () => {
    expect(
      bindingsText(
        { param_key: 'rsi_period', value_param_key: 'rsi_lower', value_ref_overrides: { slow: 'period' } },
        { period: 14 },
      ),
    ).toBe('Strategy parameter bindings: rsi_period → period, rsi_lower → value, slow → compared period');
    expect(bindingsText({ param_overrides: {} }, {})).toBe('Strategy parameter bindings: no implicit binding');
    expect(bindingsText(undefined, {})).toBeNull();
  });
});
