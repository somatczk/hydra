import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import {
  buttonByText,
  choose,
  click,
  mount,
  selectByLabel,
  unavailable,
  type Mounted,
} from '@/test/pageHarness';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { ModelVersionSelect } = await import('./ModelVersionSelect');

/** `GET /api/models`: two names, every stage, in the API's own casing. */
const MODELS = [
  { id: 'a1', name: 'alpha', version: 1, stage: 'Archived' },
  { id: 'a2', name: 'alpha', version: 2, stage: 'Production' },
  { id: 'a3', name: 'alpha', version: 3, stage: 'Staging' },
  { id: 'b1', name: 'beta', version: 1, stage: 'Staging' },
];

let page: Mounted | null = null;
let emitted: Array<string | null>;

function Harness({ names }: { names: string[] }) {
  const [value, setValue] = useState<string | null>(null);
  return (
    <ModelVersionSelect
      modelNames={names}
      value={value}
      onChange={(id) => {
        setValue(id);
        emitted.push(id);
      }}
    />
  );
}

const optionLabels = (select: HTMLSelectElement) =>
  Array.from(select.options).map((o) => [o.value, o.textContent]);

beforeEach(() => {
  emitted = [];
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('ModelVersionSelect', () => {
  it('renders nothing and reads no registry for a strategy that serves no model', async () => {
    page = await mount(<Harness names={[]} />);
    expect(page.container.innerHTML).toBe('');
    expect(fetchApi).not.toHaveBeenCalled();
  });

  it("defaults to Production and lists only that model's other versions, any stage", async () => {
    fetchApi.mockResolvedValue(MODELS);
    page = await mount(<Harness names={['alpha']} />);

    expect(fetchApi).toHaveBeenCalledWith('/api/models');
    const select = selectByLabel(page.container, 'Model version');
    expect(optionLabels(select)).toEqual([
      ['', 'Production (alpha v2)'],
      ['a1', 'alpha v1 · Archived'],
      ['a3', 'alpha v3 · Staging'],
    ]);
    expect(select.value).toBe('');
    expect(page.container.querySelector('span.inline-flex')?.textContent).toBe('Production');
  });

  it('emits the pinned id for a non-Production version and null back on Production', async () => {
    fetchApi.mockResolvedValue(MODELS);
    page = await mount(<Harness names={['alpha']} />);
    const select = selectByLabel(page.container, 'Model version');

    await choose(select, 'a3');
    expect(emitted).toEqual(['a3']);
    expect(page.container.textContent).toContain('Staging');
    expect(page.container.textContent).toContain('Pinned for this run');

    await choose(select, '');
    expect(emitted).toEqual(['a3', null]);
    expect(page.container.textContent).not.toContain('Pinned for this run');
  });

  it('names a model with no Production version, so the default cannot pass for one', async () => {
    fetchApi.mockResolvedValue(MODELS);
    page = await mount(<Harness names={['alpha', 'beta']} />);
    const select = selectByLabel(page.container, 'Model version');
    expect(select.options[0].textContent).toBe('Production (alpha v2, beta: none)');
    expect(optionLabels(select).map(([v]) => v)).toEqual(['', 'a1', 'a3', 'b1']);
    expect(page.container.querySelector('span.inline-flex')?.textContent).toBe('No Production version');

    await choose(select, 'b1');
    expect(page.container.querySelector('span.inline-flex')?.textContent).toBe('Staging');
  });

  it('shows the error envelope with a retry when the registry is unavailable', async () => {
    fetchApi.mockRejectedValueOnce(unavailable()).mockResolvedValue(MODELS);
    page = await mount(<Harness names={['alpha']} />);
    expect(page.container.textContent).toContain('Database is not available');
    expect(page.container.querySelector('[data-state="unavailable"]')).not.toBeNull();

    await click(buttonByText(page.container, 'Retry'));
    expect(selectByLabel(page.container, 'Model version').options).toHaveLength(3);
  });
});
