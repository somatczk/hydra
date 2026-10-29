import { describe, expect, it } from 'vitest';
import { reconcileTunables, storedTunables } from './tunables';
import type { BuilderCondition, BuilderState } from './types';

const EMPTY = { operator: 'AND' as const, conditions: [] as BuilderCondition[] };

function state(entryLong: BuilderCondition[], tunables: Record<string, number>, exitLong: BuilderCondition[] = []): BuilderState {
  return {
    entryLong: { operator: 'AND', conditions: entryLong },
    exitLong: { operator: 'AND', conditions: exitLong },
    entryShort: EMPTY,
    exitShort: EMPTY,
    timeframes: { primary: '1h' },
    risk: {
      stopLoss: { method: 'atr', value: 2 },
      takeProfit: { method: 'atr', value: 3 },
      sizing: { method: 'fixed_fractional', riskPerTradePct: 1, maxPositionPct: 10 },
    },
    mlOverlay: null,
    editingId: 'strat-1',
    strategyName: '',
    strategyDescription: '',
    exchangeId: 'binance',
    symbol: 'BTCUSDT',
    leverage: 1,
    schedule: null,
    tunables,
    baseline: '',
  };
}

const RSI: BuilderCondition = {
  id: 'rsi',
  indicator: 'rsi',
  params: { period: 14 },
  comparator: 'less_than',
  value: 30,
  bindings: { param_key: 'rsi_len', value_param_key: 'rsi_lower' },
};

describe('reconcileTunables', () => {
  it('writes a stored value into every site it binds, so the builder shows what runs', () => {
    const cross: BuilderCondition = {
      id: 'x',
      indicator: 'close',
      params: {},
      comparator: 'crosses_above',
      value: 'sma:period=50',
      bindings: { value_ref_overrides: { slow: 'period' } },
    };
    const next = reconcileTunables(state([RSI, cross], { rsi_len: 10, rsi_lower: 25, slow: 80 }));
    expect(next.entryLong.conditions[0]).toMatchObject({ params: { period: 10 }, value: 25 });
    expect(next.entryLong.conditions[1].value).toBe('sma:period=80');
  });

  it('moves a tunable with a user edit of a bound value, and writes it to the name\'s other sites', () => {
    const twin: BuilderCondition = { ...RSI, id: 'twin', bindings: { param_overrides: { rsi_len: 'period' } } };
    const loaded = reconcileTunables(state([RSI], { rsi_len: 10 }, [twin]));
    const edited = {
      ...loaded,
      entryLong: { ...loaded.entryLong, conditions: [{ ...loaded.entryLong.conditions[0], params: { period: 21 } }] },
    };
    const next = reconcileTunables(edited, { section: 'entryLong', conditionId: 'rsi' });
    expect(next.tunables).toEqual({ rsi_len: 21 });
    expect(next.exitLong.conditions[0].params).toEqual({ period: 21 });
  });

  it('keeps the stored value over a reset that is not a user edit (an indicator change, a new condition)', () => {
    const implicit: BuilderCondition = { id: 'new', indicator: 'rsi', params: { period: 14 }, comparator: 'less_than', value: 30 };
    const next = reconcileTunables(state([implicit], { rsi_period: 9 }));
    expect(next.tunables).toEqual({ rsi_period: 9 });
    expect(next.entryLong.conditions[0].params).toEqual({ period: 9 });
  });

  it('drops the stored value of a name nothing binds any more', () => {
    const unbound = { ...RSI, bindings: { value_param_key: 'rsi_lower' } };
    const next = reconcileTunables(state([unbound], { rsi_len: 10, rsi_lower: 25 }));
    expect(next.tunables).toEqual({ rsi_lower: 25 });
    expect(next.entryLong.conditions[0].params).toEqual({ period: 14 });
  });

  it('reads only numeric stored parameters', () => {
    expect(storedTunables({ rsi_len: 10, rsi_lower: 25.5, note: 'x', flag: true })).toEqual({ rsi_len: 10, rsi_lower: 25.5 });
    expect(storedTunables(undefined)).toEqual({});
  });
});
