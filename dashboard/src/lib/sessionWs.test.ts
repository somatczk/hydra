import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { classifySessionWsMessage, debounce } from './sessionWs';

// Exact shape published by session_manager.on_fill_ws (all numbers are strings)
const orderFill = {
  event: 'order_fill',
  session_id: 's1',
  order_id: 'ord-42',
  symbol: 'BTCUSDT',
  side: 'BUY',
  quantity: '0.01',
  price: '65000',
  fee: '0.65',
  timestamp: '2026-10-20T12:00:00+00:00',
};

describe('classifySessionWsMessage', () => {
  it('refetches on a fill for this session', () => {
    expect(classifySessionWsMessage(orderFill, 's1')).toBe('refetch');
  });

  it('ignores subscription acks', () => {
    expect(classifySessionWsMessage({ ack: true, channel: 'trades:s1' }, 's1')).toBe('ignore');
  });

  it('ignores fills for other sessions', () => {
    expect(classifySessionWsMessage({ ...orderFill, session_id: 's2' }, 's1')).toBe('ignore');
  });

  it('ignores malformed messages', () => {
    expect(classifySessionWsMessage(null, 's1')).toBe('ignore');
    expect(classifySessionWsMessage('order_fill', 's1')).toBe('ignore');
    expect(classifySessionWsMessage(42, 's1')).toBe('ignore');
    expect(classifySessionWsMessage({ event: 'order_fill' }, 's1')).toBe('ignore');
  });
});

describe('debounce', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('collapses a burst of fills into one refetch', () => {
    const fetchDetail = vi.fn();
    const d = debounce(fetchDetail, 500);
    d.trigger();
    vi.advanceTimersByTime(200);
    d.trigger();
    vi.advanceTimersByTime(200);
    d.trigger();
    expect(fetchDetail).not.toHaveBeenCalled();
    vi.advanceTimersByTime(500);
    expect(fetchDetail).toHaveBeenCalledTimes(1);
  });

  it('fires again for a later fill', () => {
    const fetchDetail = vi.fn();
    const d = debounce(fetchDetail, 500);
    d.trigger();
    vi.advanceTimersByTime(500);
    d.trigger();
    vi.advanceTimersByTime(500);
    expect(fetchDetail).toHaveBeenCalledTimes(2);
  });

  it('cancel drops a pending refetch (unmount)', () => {
    const fetchDetail = vi.fn();
    const d = debounce(fetchDetail, 500);
    d.trigger();
    d.cancel();
    vi.advanceTimersByTime(1000);
    expect(fetchDetail).not.toHaveBeenCalled();
  });
});
