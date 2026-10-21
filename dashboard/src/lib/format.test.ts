import { describe, expect, it } from 'vitest';
import { age, duration, limitPct, money, pct, price, qty, signed, signedLabel, utc } from './format';

const NOW = Date.parse('2026-10-21T14:05:00Z');

describe('format', () => {
  it.each([money, price, signed, signedLabel, pct, limitPct, qty, age, duration])(
    '%o returns null for null, undefined and NaN',
    (fn) => {
      expect(fn(null)).toBeNull();
      expect(fn(undefined)).toBeNull();
      expect(fn(Number.NaN)).toBeNull();
    },
  );

  it('formats money and price with 2 dp and thousands', () => {
    expect(money(9512.4)).toBe('9,512.40');
    expect(price(67638.123)).toBe('67,638.12');
    expect(money(0)).toBe('0.00');
  });

  it('signs with + and U+2212, zero unsigned', () => {
    expect(signed(124.1)).toBe('+124.10');
    expect(signed(-38.62)).toBe('−38.62');
    expect(signed(0)).toBe('0.00');
    expect(signed(-0.001)).toBe('0.00');
    expect(signedLabel(124.1)).toBe('gain 124.10 USDT');
    expect(signedLabel(-38.62)).toBe('loss 38.62 USDT');
  });

  it('formats percents that are already ×100 and limits exactly', () => {
    expect(pct(1.84)).toBe('1.84%');
    expect(pct(0.42, { signed: true })).toBe('+0.42%');
    expect(pct(-0.42, { signed: true })).toBe('−0.42%');
    expect(limitPct(0.005)).toBe('0.5%');
    expect(limitPct(0.15)).toBe('15%');
  });

  it('never rounds a non-zero quantity to zero', () => {
    expect(qty(0.0015)).toBe('0.00150');
    expect(qty(0.0000042)).toBe('0.00000420');
    expect(qty(0)).toBe('0.00000');
    expect(qty(1.5)).toBe('1.50000');
  });

  it('labels timestamps UTC for today, this year and other years', () => {
    expect(utc('2026-10-21T14:02:10Z', NOW)).toBe('14:02:10 UTC');
    expect(utc('2026-10-18T14:02:59Z', NOW)).toBe('18 Oct 14:02 UTC');
    expect(utc('2025-10-18T14:02:00Z', NOW)).toBe('18 Oct 2025 14:02 UTC');
    expect(utc('2026-10-21T00:30:00+02:00', NOW)).toBe('20 Oct 22:30 UTC');
    expect(utc(null, NOW)).toBeNull();
    expect(utc('not a date', NOW)).toBeNull();
  });

  it('formats ages at their boundaries', () => {
    expect(age(40_000, NOW)).toBe('40 s ago');
    expect(age(59_999, NOW)).toBe('59 s ago');
    expect(age(60_000, NOW)).toBe('1 min ago');
    expect(age(59 * 60_000, NOW)).toBe('59 min ago');
    expect(age(60 * 60_000, NOW)).toBe('1 h ago');
    expect(age(24 * 3_600_000 - 1, NOW)).toBe('23 h ago');
    expect(age(24 * 3_600_000, NOW)).toBe('20 Oct 14:05 UTC');
  });

  it('formats durations', () => {
    expect(duration(40_000)).toBe('40 s');
    expect(duration(14 * 60_000)).toBe('14 min');
    expect(duration(2 * 3_600_000 + 14 * 60_000)).toBe('2 h 14 min');
    expect(duration(3 * 86_400_000 + 4 * 3_600_000)).toBe('3 d 4 h');
  });
});
