import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { click, mount, type Mounted } from '@/test/pageHarness';
import { ConditionRow } from './ConditionRow';
import type { BuilderCondition, ComparatorSchema, IndicatorSchema } from './types';

const INDICATORS: IndicatorSchema[] = [
  {
    name: 'rsi',
    category: 'momentum',
    description: 'Relative Strength Index (0-100 oscillator)',
    params: [{ name: 'period', type: 'int', default: 14, min: 2, max: 200 }],
  },
];

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
});
