import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { advance, click, mount, routeFetch, unavailable, type Mounted } from '@/test/pageHarness';
import { liveStatus } from '@/test/fixtures';

const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { usePoll } = await import('./usePoll');

const PATH = '/api/risk/live-status';

function Probe({ path }: { path: string | null }) {
  const p = usePoll<{ running_sessions: number | null }>(path, 10_000);
  return (
    <>
      <output data-state={p.state} data-fetched={p.fetchedAt ?? ''}>
        {p.data ? String(p.data.running_sessions) : 'none'}
      </output>
      <button onClick={() => void p.refetch()}>refetch</button>
    </>
  );
}

let m: Mounted;
let calls: Array<{ path: string }>;
let response: unknown;

const out = () => m.container.querySelector('output') as HTMLOutputElement;
const setHidden = (hidden: boolean) => {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'));
  });
};

beforeEach(() => {
  vi.useFakeTimers();
  calls = [];
  response = liveStatus({ running_sessions: 2 });
  fetchApi.mockImplementation(routeFetch({ [PATH]: () => response }, calls));
});

afterEach(() => {
  m.unmount();
  setHidden(false);
  vi.useRealTimers();
  fetchApi.mockReset();
});

describe('usePoll', () => {
  it('goes loading → ok and polls on the interval', async () => {
    let resolve: (v: unknown) => void = () => {};
    fetchApi.mockImplementationOnce(() => new Promise((r) => (resolve = r)));
    m = await mount(<Probe path={PATH} />);
    expect(out().dataset.state).toBe('loading');
    await act(async () => resolve(liveStatus({ running_sessions: 2 })));
    expect(out().dataset.state).toBe('ok');
    expect(out().textContent).toBe('2');
    await advance(10_000);
    expect(fetchApi).toHaveBeenCalledTimes(2);
  });

  it('keeps ok and the value after one failure, and turns stale after two', async () => {
    m = await mount(<Probe path={PATH} />);
    const fetchedAt = out().dataset.fetched;
    response = unavailable();
    await advance(10_000);
    expect(out().dataset.state).toBe('ok');
    expect(out().textContent).toBe('2');
    await advance(10_000);
    expect(out().dataset.state).toBe('stale');
    expect(out().textContent).toBe('2');
    expect(out().dataset.fetched).toBe(fetchedAt);
    response = liveStatus({ running_sessions: 3 });
    await advance(10_000);
    expect(out().dataset.state).toBe('ok');
    expect(out().textContent).toBe('3');
  });

  it('counts a poll still unanswered at the next tick as a miss and aborts it (a hung engine turns stale)', async () => {
    m = await mount(<Probe path={PATH} />);
    const fetchedAt = out().dataset.fetched;
    fetchApi.mockImplementation(() => new Promise(() => {}));
    await advance(10_000);
    await advance(10_000);
    expect(out().dataset.state).toBe('refreshing');
    await advance(10_000);
    expect(out().dataset.state).toBe('stale');
    expect(out().textContent).toBe('2');
    expect(out().dataset.fetched).toBe(fetchedAt);
    const signals = fetchApi.mock.calls.map((c) => (c[1] as RequestInit).signal as AbortSignal);
    expect(signals).toHaveLength(4);
    expect(signals.slice(1, 3).map((x) => x.aborted)).toEqual([true, true]);
    expect(signals[3].aborted).toBe(false);
  });

  it('drops an older poll that answers after a newer refetch', async () => {
    m = await mount(<Probe path={PATH} />);
    let answerOld: (v: unknown) => void = () => {};
    fetchApi.mockImplementationOnce(() => new Promise((r) => (answerOld = r)));
    await advance(10_000);
    fetchApi.mockImplementationOnce(async () => liveStatus({ running_sessions: 3 }));
    await click(m.container.querySelector('button') as HTMLButtonElement);
    expect(out().textContent).toBe('3');
    await act(async () => answerOld(liveStatus({ running_sessions: 5 })));
    expect(out().textContent).toBe('3');
    expect(out().dataset.state).toBe('ok');
  });

  it('maps a first failure with no data to the envelope state', async () => {
    response = unavailable();
    m = await mount(<Probe path={PATH} />);
    expect(out().dataset.state).toBe('unavailable');
    expect(out().textContent).toBe('none');
  });

  it('pauses while the document is hidden and refetches on wake', async () => {
    m = await mount(<Probe path={PATH} />);
    setHidden(true);
    await advance(60_000);
    expect(fetchApi).toHaveBeenCalledTimes(1);
    response = liveStatus({ running_sessions: 0 });
    setHidden(false);
    await advance(0);
    expect(fetchApi).toHaveBeenCalledTimes(2);
    expect(out().textContent).toBe('0');
  });

  it('a null path does not poll', async () => {
    m = await mount(<Probe path={null} />);
    await advance(30_000);
    expect(fetchApi).not.toHaveBeenCalled();
  });
});
