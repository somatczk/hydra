import { describe, expect, it } from 'vitest';
import { chartColors, formatYTick } from './chart-utils';

describe('chartColors', () => {
  it('falls back to the globals.css hexes when the CSS variables are unreadable (tests)', () => {
    expect(chartColors()).toEqual({
      accent: '#3949c7',
      caution: '#8a5700',
      muted: '#8a8d97',
      grid: '#e3e5ea',
      text: '#555863',
    });
  });

  it('reads the tokens from the document at render time', () => {
    document.documentElement.style.setProperty('--accent', '#112233');
    try {
      expect(chartColors().accent).toBe('#112233');
    } finally {
      document.documentElement.style.removeProperty('--accent');
    }
  });
});

describe('formatYTick', () => {
  it('carries no currency symbol', () => {
    expect(formatYTick(12500)).toBe('13k');
    expect(formatYTick(12500)).not.toContain('$');
  });
});
