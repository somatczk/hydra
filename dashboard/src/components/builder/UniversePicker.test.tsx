import { act, useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flush, mount, routeFetch, typeInto, unavailable, type Mounted, type Routes } from '@/test/pageHarness';
import { UniversePicker, type UniverseSnapshot } from './UniversePicker';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

/** The stored ranking exactly as GET /api/market/universe serves it. */
const UNIVERSE: UniverseSnapshot = {
  exchange_id: 'binance',
  quote_asset: 'USDT',
  source: 'binance spot rolling-24h quote volume (GET /api/v3/ticker/24hr)',
  ranked_at: '2026-10-29T08:00:00+00:00',
  age_hours: 2,
  count: 3,
  symbols: [
    { rank: 1, symbol: 'BTCUSDT', quote_volume: 2_000_000_000 },
    { rank: 2, symbol: 'ETHUSDT', quote_volume: 800_000_000 },
    { rank: 3, symbol: 'SOLUSDT', quote_volume: 300_000_000 },
  ],
};

let m: Mounted | null = null;
let symbol: string;
let loaded: Array<UniverseSnapshot | null>;

beforeEach(() => {
  fetchApi.mockReset();
  symbol = 'BTCUSDT';
  loaded = [];
});

afterEach(() => {
  m?.unmount();
  m = null;
});

async function mountPicker(
  routes: Routes = { '/api/market/universe': UNIVERSE },
  initialSymbol = 'BTCUSDT',
): Promise<void> {
  fetchApi.mockImplementation(routeFetch(routes, []));
  function Harness() {
    const [value, setValue] = useState(initialSymbol);
    return (
      <UniversePicker
        open
        value={value}
        onChange={(s) => {
          setValue(s);
          symbol = s;
        }}
        onUniverse={(snapshot) => loaded.push(snapshot)}
      />
    );
  }
  m = await mount(<Harness />);
  await flush();
}

const input = () => m!.container.querySelector('input') as HTMLInputElement;
const listbox = () => m!.container.querySelector('[role="listbox"]');
const options = () => Array.from(m!.container.querySelectorAll('[role="option"]'));

async function focusInput(): Promise<void> {
  await act(async () => {
    input().focus();
  });
  await flush();
}

async function press(key: string): Promise<void> {
  await act(async () => {
    input().dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
  });
  await flush();
}

describe('UniversePicker', () => {
  it('reads the stored ranking on open and reports it to the owner', async () => {
    await mountPicker();

    expect(input().value).toBe('BTCUSDT');
    expect(loaded).toEqual([null, UNIVERSE]);
    expect(m!.container.textContent).toContain('Top 3 binance USDT pairs by 24h quote volume · ranked 2 h ago');
  });

  it('exposes the combobox pattern: expanded state, a labelled listbox, activedescendant, aria-selected', async () => {
    await mountPicker();
    await focusInput();

    const el = input();
    expect(el.getAttribute('role')).toBe('combobox');
    expect(el.getAttribute('aria-expanded')).toBe('true');
    expect(el.getAttribute('aria-controls')).toBe(listbox()!.id);
    expect(el.getAttribute('aria-autocomplete')).toBe('list');
    expect(el.getAttribute('aria-activedescendant')).toBe(options()[0].id);
    expect(listbox()!.getAttribute('role')).toBe('listbox');
    expect(options().map((o) => o.getAttribute('aria-selected'))).toEqual(['true', 'false', 'false']);
  });

  it('searches the members by substring and commits only the highlighted member on Enter', async () => {
    await mountPicker();
    await focusInput();

    await typeInto(input(), 'SOL');
    expect(options().map((o) => o.textContent)).toEqual(['#3SOLUSDT24h 300M']);

    await press('Enter');
    expect(symbol).toBe('SOLUSDT');
    expect(input().getAttribute('aria-expanded')).toBe('false');
  });

  it('navigates with the arrow keys and wraps neither way', async () => {
    await mountPicker();
    await focusInput();

    await press('ArrowDown');
    expect(input().getAttribute('aria-activedescendant')).toBe(options()[1].id);
    await press('ArrowDown');
    await press('ArrowDown');
    expect(input().getAttribute('aria-activedescendant')).toBe(options()[2].id);
    await press('ArrowUp');
    expect(input().getAttribute('aria-activedescendant')).toBe(options()[1].id);

    await press('Enter');
    expect(symbol).toBe('ETHUSDT');
  });

  it('never offers a non-member: a no-match search leaves nothing to select and says why', async () => {
    await mountPicker();
    await focusInput();

    await typeInto(input(), 'FOO');
    expect(options()).toHaveLength(0);
    expect(m!.container.textContent).toContain(
      'No member matches "FOO". Symbols outside the top-100 universe can\'t be saved.',
    );

    await press('Enter');
    expect(symbol).toBe('FOO');
    expect(m!.container.textContent).toContain('FOO is not in the current top-100 universe — pick a member from the list.');
    expect(input().getAttribute('aria-invalid')).toBe('true');
  });

  it('matches the backend normalization exactly: the slash form is a member, case is not folded', async () => {
    // typing canonicalizes to uppercase like the input always did, so the committed draft
    // is exactly what the engine sees; the slash form the backend normalizes reads as a member
    await mountPicker();
    await focusInput();

    await typeInto(input(), 'BTC/USDT');
    expect(m!.container.textContent).not.toContain('is not in the current top-100 universe');
    expect(symbol).toBe('BTC/USDT');

    // a draft that arrives lowercase (a hand-edited stored strategy) is judged exactly as the
    // engine judges it: solusdt is a non-member, because the gate never case-folds
    m?.unmount();
    await mountPicker({ '/api/market/universe': UNIVERSE }, 'solusdt');
    expect(m!.container.textContent).toContain(
      'solusdt is not in the current top-100 universe — pick a member from the list.',
    );
    expect(input().getAttribute('aria-invalid')).toBe('true');
  });

  it('closes the list on Escape and keeps the draft', async () => {
    await mountPicker();
    await focusInput();

    await press('Escape');
    expect(input().getAttribute('aria-expanded')).toBe('false');
    expect(symbol).toBe('BTCUSDT');
    expect(options()).toHaveLength(0);
  });

  it('degrades to a plain field with the honest reason when the universe cannot be read', async () => {
    await mountPicker({ '/api/market/universe': unavailable('The top-100 universe has not been computed yet') });

    expect(loaded).toEqual([null, null]);
    expect(input().getAttribute('aria-expanded')).toBe('false');
    await focusInput();
    expect(listbox()).toBeNull();
    expect(m!.container.textContent).toContain(
      'Universe unavailable: The top-100 universe has not been computed yet — the engine still refuses a non-member symbol on save.',
    );
  });

  it('fetches nothing while its dialog is closed', async () => {
    fetchApi.mockImplementation(routeFetch({}, []));
    m = await mount(
      <UniversePicker
        open={false}
        value={symbol}
        onChange={() => {}}
        onUniverse={(snapshot) => loaded.push(snapshot)}
      />,
    );
    await flush();

    expect(fetchApi).not.toHaveBeenCalled();
  });
});
