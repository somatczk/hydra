import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { click, inputByLabel, mount, typeInto, type Mounted } from '@/test/pageHarness';
import { ConditionRow, formatIndicatorRef, parseIndicatorRef } from './ConditionRow';
import type { BuilderCondition, ComparatorSchema, IndicatorSchema } from './types';

const INDICATORS: IndicatorSchema[] = [
  {
    name: 'rsi',
    category: 'momentum',
    description: 'Relative Strength Index (0-100 oscillator)',
    params: [{ name: 'period', type: 'int', default: 14, min: 2, max: 200 }],
  },
];

/** Registry rows as GET /api/strategies/indicators serves them. */
const MACD: IndicatorSchema = {
  name: 'macd',
  category: 'trend',
  description: 'MACD line (fast EMA - slow EMA)',
  params: [
    { name: 'fast', type: 'int', default: 12, min: 2, max: 100 },
    { name: 'slow', type: 'int', default: 26, min: 2, max: 200 },
    { name: 'signal', type: 'int', default: 9, min: 2, max: 100 },
  ],
};
const CLOSE: IndicatorSchema = { name: 'close', category: 'other', description: 'close indicator', params: [] };
const LAG: IndicatorSchema = {
  name: 'lag',
  category: 'trend',
  description: 'Close N bars ago (time-series momentum: close > lag)',
  params: [{ name: 'period', type: 'int', default: null, min: 2, max: 200 }],
};

function selectByLabel(container: HTMLElement, label: string): HTMLSelectElement {
  const el = Array.from(container.querySelectorAll('label')).find((l) => l.textContent === label);
  return document.getElementById(el!.htmlFor) as HTMLSelectElement;
}

async function choose(select: HTMLSelectElement, value: string): Promise<void> {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
  await act(async () => {
    setter?.call(select, value);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

const COMPARATORS: ComparatorSchema[] = [
  { value: 'less_than', label: 'Less Than', description: 'Value is less than target' },
];

const CONDITION: BuilderCondition = {
  id: 'c1',
  indicator: 'rsi',
  params: { period: 14 },
  comparator: 'less_than',
  value: 30,
};

let page: Mounted | null = null;

beforeEach(() => {
  page = null;
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('ConditionRow', () => {
  it('gives the remove button a 44px square target below 1024 and keeps it keyboard operable', async () => {
    const onRemove = vi.fn();
    page = await mount(
      <ConditionRow condition={CONDITION} indicators={INDICATORS} comparators={COMPARATORS} onUpdate={vi.fn()} onRemove={onRemove} />,
    );

    const remove = page.container.querySelector('button[aria-label="Remove condition"]') as HTMLButtonElement;
    expect(remove).not.toBeNull();
    expect(remove.className).toContain('max-lg:h-11');
    expect(remove.className).toContain('max-lg:w-11');

    remove.focus();
    expect(document.activeElement).toBe(remove);
    await click(remove);
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it('stacks the row fields at base width and keeps the p-3 inset for the 320px reflow', async () => {
    page = await mount(
      <ConditionRow condition={CONDITION} indicators={INDICATORS} comparators={COMPARATORS} onUpdate={vi.fn()} onRemove={vi.fn()} />,
    );

    const row = page.container.firstElementChild as HTMLElement;
    expect(row.className).toContain('p-3');
    expect(row.className).toContain('border-line');
    expect(row.className).toContain('bg-surface');
    const grid = row.querySelector('.grid') as HTMLElement;
    expect(grid.className).toMatch(/(^|\s)grid-cols-1(\s|$)/);
    expect(grid.className).toContain('md:grid-cols-12');
    expect(grid.className).not.toMatch(/(^|\s)grid-cols-(2|3|12)(\s|$)/);
  });

  it('replaces the previous indicator\'s params with the new one\'s defaults (rsi period on macd is a 422)', async () => {
    const onUpdate = vi.fn();
    page = await mount(
      <ConditionRow condition={CONDITION} indicators={[...INDICATORS, MACD]} comparators={COMPARATORS} onUpdate={onUpdate} onRemove={vi.fn()} />,
    );

    await choose(selectByLabel(page.container, 'Indicator'), 'macd');
    expect(onUpdate.mock.calls).toEqual([
      ['indicator', 'macd'],
      ['params', { fast: 12, slow: 26, signal: 9 }],
    ]);
  });

  it('compares against another indicator: close greater_than lag:period=60 shows and edits the reference', async () => {
    const onUpdate = vi.fn();
    const condition: BuilderCondition = { id: 'c2', indicator: 'close', params: {}, comparator: 'less_than', value: 'lag:period=60' };
    page = await mount(
      <ConditionRow condition={condition} indicators={[CLOSE, LAG]} comparators={COMPARATORS} onUpdate={onUpdate} onRemove={vi.fn()} />,
    );

    expect(selectByLabel(page.container, 'Compare to').value).toBe('indicator');
    expect(selectByLabel(page.container, 'Compared indicator').value).toBe('lag');
    const period = inputByLabel(page.container, 'period');
    expect(period.value).toBe('60');
    expect(page.container.textContent).not.toContain('Value');

    await typeInto(period, '30');
    expect(onUpdate).toHaveBeenLastCalledWith('value', 'lag:period=30');
  });

  it('switches the comparison between a number and an indicator', async () => {
    const onUpdate = vi.fn();
    page = await mount(
      <ConditionRow condition={CONDITION} indicators={[...INDICATORS, CLOSE]} comparators={COMPARATORS} onUpdate={onUpdate} onRemove={vi.fn()} />,
    );

    expect(inputByLabel(page.container, 'Value').value).toBe('30');
    await choose(selectByLabel(page.container, 'Compare to'), 'indicator');
    expect(onUpdate).toHaveBeenLastCalledWith('value', 'close');
  });

  it('shows the stored bindings, and drops and names the ones an indicator change invalidates', async () => {
    const onUpdate = vi.fn();
    const condition: BuilderCondition = {
      ...CONDITION,
      bindings: { param_key: 'rsi_period', value_param_key: 'rsi_lower' },
    };
    page = await mount(
      <ConditionRow condition={condition} indicators={[...INDICATORS, MACD]} comparators={COMPARATORS} onUpdate={onUpdate} onRemove={vi.fn()} />,
    );

    expect(page.container.querySelector('[data-testid="condition-bindings"]')?.textContent).toBe(
      'Strategy parameter bindings: rsi_period → period, rsi_lower → value',
    );
    await choose(selectByLabel(page.container, 'Indicator'), 'macd');
    expect(onUpdate.mock.calls).toEqual([
      ['indicator', 'macd'],
      ['params', { fast: 12, slow: 26, signal: 9 }],
      ['bindings', { value_param_key: 'rsi_lower' }],
    ]);
    expect(page.container.querySelector('[role="status"]')?.textContent).toContain('Dropped rsi_period: it bound');
  });

  it('drops the value-side binding when the comparison switches from a number to an indicator', async () => {
    const onUpdate = vi.fn();
    const condition: BuilderCondition = { ...CONDITION, bindings: { param_key: 'rsi_period', value_param_key: 'rsi_lower' } };
    page = await mount(
      <ConditionRow condition={condition} indicators={[...INDICATORS, CLOSE]} comparators={COMPARATORS} onUpdate={onUpdate} onRemove={vi.fn()} />,
    );

    await choose(selectByLabel(page.container, 'Compare to'), 'indicator');
    expect(onUpdate.mock.calls).toEqual([
      ['value', 'close'],
      ['bindings', { param_key: 'rsi_period' }],
    ]);
  });
});

describe('indicator references', () => {
  it('round-trips the rule engine\'s "name:k=v,k=v" form', () => {
    const ref = 'keltner_upper:ema_period=20,atr_period=10,multiplier=1.5';
    expect(parseIndicatorRef(ref)).toEqual({ name: 'keltner_upper', params: { ema_period: 20, atr_period: 10, multiplier: 1.5 } });
    expect(formatIndicatorRef('keltner_upper', parseIndicatorRef(ref).params)).toBe(ref);
    expect(parseIndicatorRef('close')).toEqual({ name: 'close', params: {} });
    expect(formatIndicatorRef('close', {})).toBe('close');
  });
});
