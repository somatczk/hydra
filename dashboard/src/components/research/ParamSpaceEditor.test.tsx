import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { useState } from 'react';
import type { StrategyParamDef, WireParamDef } from '@/lib/research';
import { buttonByText, click, mount, typeInto, type Mounted } from '@/test/pageHarness';
import { ParamSpaceEditor } from '@/components/research/ParamSpaceEditor';

const DEFS: StrategyParamDef[] = [
  { name: 'sma_period', label: 'SMA Period', type: 'int', default: 5, min: 2, max: 20, step: 1 },
  { name: 'threshold', label: 'Threshold', type: 'float', default: 0.5, min: 0.1, max: 2, step: 0.1 },
];

const OTHER_DEFS: StrategyParamDef[] = [
  { name: 'rsi_period', label: 'RSI Period', type: 'int', default: 14, min: 5, max: 30, step: 1 },
];

let emitted: Array<WireParamDef[] | null> = [];

/** Stands in for the page: the submit is enabled only while the editor emits a valid space. */
function Harness({ defs }: { defs: StrategyParamDef[] | null }) {
  const [space, setSpace] = useState<WireParamDef[] | null>(null);
  return (
    <div>
      <ParamSpaceEditor
        defs={defs}
        onChange={(s) => {
          setSpace(s);
          emitted.push(s);
        }}
      />
      <button type="button" disabled={space === null}>
        Submit
      </button>
    </div>
  );
}

let page: Mounted | null = null;

beforeEach(() => {
  emitted = [];
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('ParamSpaceEditor', () => {
  it('renders each definition with its default hint, and emits the wire param_space', async () => {
    page = await mount(<Harness defs={DEFS} />);

    const text = page.container.textContent ?? '';
    expect(text).toContain('SMA Period');
    expect(text).toContain('default 5');
    expect(text).toContain('Threshold');
    expect(text).toContain('default 0.5');

    expect(emitted.at(-1)).toEqual([
      { name: 'sma_period', type: 'int', low: 2, high: 20 },
      { name: 'threshold', type: 'float', low: 0.1, high: 2 },
    ]);
    expect(JSON.stringify(emitted.at(-1))).not.toContain('step');
    expect(buttonByText(page.container, 'Submit').disabled).toBe(false);
  });

  it('says it is reading while the definitions load and emits nothing usable', async () => {
    page = await mount(<Harness defs={null} />);

    expect(page.container.textContent).toContain('Reading parameters…');
    expect(emitted.at(-1)).toBeNull();
    expect(buttonByText(page.container, 'Submit').disabled).toBe(true);
  });

  it('says when a strategy has no optimisable parameters', async () => {
    page = await mount(<Harness defs={[]} />);

    expect(page.container.textContent).toContain('This strategy has no optimisable parameters.');
    expect(emitted.at(-1)).toBeNull();
    expect(buttonByText(page.container, 'Submit').disabled).toBe(true);
  });

  it('flags a non-ascending range inline and disables the parent submit until fixed', async () => {
    page = await mount(<Harness defs={DEFS} />);

    const max = page.container.querySelector<HTMLInputElement>('[aria-label="SMA Period max"]')!;
    await typeInto(max, '1');

    expect(page.container.textContent).toContain('Max must be greater than min.');
    expect(emitted.at(-1)).toBeNull();
    expect(buttonByText(page.container, 'Submit').disabled).toBe(true);

    await typeInto(max, '20');
    expect(page.container.textContent).not.toContain('Max must be greater than min.');
    expect(emitted.at(-1)).toEqual([
      { name: 'sma_period', type: 'int', low: 2, high: 20 },
      { name: 'threshold', type: 'float', low: 0.1, high: 2 },
    ]);
    expect(buttonByText(page.container, 'Submit').disabled).toBe(false);
  });

  it('re-seeds the ranges when the definitions change', async () => {
    page = await mount(<SwappingHarness />);

    const max = page.container.querySelector<HTMLInputElement>('[aria-label="SMA Period max"]')!;
    await typeInto(max, '9');
    expect(emitted.at(-1)).toEqual([
      { name: 'sma_period', type: 'int', low: 2, high: 9 },
      { name: 'threshold', type: 'float', low: 0.1, high: 2 },
    ]);

    await click(buttonByText(page.container, 'Switch strategy'));
    expect(page.container.textContent).toContain('RSI Period');
    expect(emitted.at(-1)).toEqual([{ name: 'rsi_period', type: 'int', low: 5, high: 30 }]);
  });
});

/** The page-level re-seed flow: picking another strategy hands the editor a new defs array. */
function SwappingHarness() {
  const [alt, setAlt] = useState(false);
  const [space, setSpace] = useState<WireParamDef[] | null>(null);
  return (
    <div>
      <ParamSpaceEditor
        defs={alt ? OTHER_DEFS : DEFS}
        onChange={(s) => {
          setSpace(s);
          emitted.push(s);
        }}
      />
      <button type="button" onClick={() => setAlt(true)}>
        Switch strategy
      </button>
      <button type="button" disabled={space === null}>
        Submit
      </button>
    </div>
  );
}
