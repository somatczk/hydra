import { describe, expect, it } from 'vitest';
import parity from '@/test/paramBindings.parity.json';
import {
  ambiguous,
  bindableParams,
  bindingsOf,
  bindingsText,
  dropBindings,
  paramBindings,
  RULE_GROUPS,
  unknownTargets,
  type Catalogue,
} from './bindings';
import type { BuilderState, RuleSection } from './types';

type ApiCondition = { indicator: string; params: Record<string, number>; value: number | string } & Record<string, unknown>;

/** The fixture's API-shaped rules as builder groups, the way the builder loads a stored strategy. */
function builderRules(rules: Record<string, { conditions: ApiCondition[] } | null>): Pick<BuilderState, RuleSection> {
  return Object.fromEntries(
    RULE_GROUPS.map(([group, section]) => [
      section,
      {
        operator: 'AND',
        conditions: (rules[group]?.conditions ?? []).map((c, i) => ({
          id: `${group}-${i}`,
          indicator: c.indicator,
          params: c.params,
          comparator: 'less_than',
          value: c.value,
          bindings: bindingsOf(c),
        })),
      },
    ]),
  ) as Pick<BuilderState, RuleSection>;
}

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

describe('the ported resolver', () => {
  it('matches strategy/builtin/rule_based.py param_bindings on the shared fixture', () => {
    const rules = builderRules(parity.rules as Record<string, { conditions: ApiCondition[] } | null>);
    expect(bindableParams(rules, parity.catalogue)).toEqual(parity.bindable);
    const table = paramBindings(rules, parity.catalogue);
    expect(Object.keys(table).filter((name) => ambiguous(table[name])).sort()).toEqual(parity.ambiguous);
  });

  it('flags the param_overrides target the engine refuses at save, at the same site', () => {
    const { rules, site } = parity.refused;
    const builder = builderRules(rules as Record<string, { conditions: ApiCondition[] } | null>);
    const catalogue: Catalogue = parity.catalogue;
    const known = (name: string) => (catalogue[name] ? Object.keys(catalogue[name]) : undefined);
    const flagged = RULE_GROUPS.flatMap(([group, section]) =>
      builder[section].conditions.flatMap((c, index) =>
        unknownTargets(c.bindings?.param_overrides, known(c.indicator)).map((name) => ({
          group,
          index,
          name,
          key: c.bindings?.param_overrides?.[name],
        })),
      ),
    );
    expect(flagged).toEqual([site]);
  });

  it('leaves an indicator the catalogue does not list to the engine', () => {
    expect(unknownTargets({ x: 'length' }, undefined)).toEqual([]);
    expect(unknownTargets({}, ['period'])).toEqual([]);
  });

  it('without the catalogue binds an unknown compared key and an absent default as undefined', () => {
    // why the parity needs the registry: the engine skips bbu_typo and binds the 2.0 signature defaults
    const rules = builderRules(parity.rules as Record<string, { conditions: ApiCondition[] } | null>);
    const bare = paramBindings(rules);
    expect(bare.bbu_typo).toHaveLength(1);
    expect(bare.bbl_dev[0].value).toBeUndefined();
    expect(paramBindings(rules, parity.catalogue).bbu_typo).toBeUndefined();
  });
});

