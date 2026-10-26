export type TimeResolution = 'hours' | 'days' | 'months';

export interface EquityPoint {
  timestamp: string;
  value: number;
}

export interface ChartColors {
  /** Primary series: strategy equity, compare run A. */
  accent: string;
  /** Second series: compare run B. */
  caution: string;
  /** Reference series: benchmark equity. */
  muted: string;
  grid: string;
  text: string;
}

/** globals.css light-theme hexes, used when the CSS variables cannot be read (test environments). */
const COLOR_TOKENS: Record<keyof ChartColors, [string, string]> = {
  accent: ['--accent', '#3949c7'],
  caution: ['--caution', '#8a5700'],
  muted: ['--line-strong', '#8a8d97'],
  grid: ['--line', '#e3e5ea'],
  text: ['--ink-2', '#555863'],
};

/** Design tokens read at render time, so a theme switch is reflected; hexes when the variables are absent. */
export function chartColors(): ChartColors {
  const style = typeof document === 'undefined' ? null : getComputedStyle(document.documentElement);
  const read = (name: keyof ChartColors): string => {
    const [token, hex] = COLOR_TOKENS[name];
    return style?.getPropertyValue(token).trim() || hex;
  };
  return {
    accent: read('accent'),
    caution: read('caution'),
    muted: read('muted'),
    grid: read('grid'),
    text: read('text'),
  };
}

export function aggregateEquityCurve(data: EquityPoint[], resolution: TimeResolution): EquityPoint[] {
  if (resolution === 'hours') return data;

  const groups = new Map<string, EquityPoint>();
  for (const point of data) {
    const d = new Date(point.timestamp);
    const key = resolution === 'days'
      ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
      : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    groups.set(key, point);
  }
  return Array.from(groups.values());
}

export function formatXTick(timestamp: string, resolution: TimeResolution = 'days'): string {
  const d = new Date(timestamp);
  if (resolution === 'hours') {
    return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
  }
  if (resolution === 'months') {
    return d.toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
  }
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

export function formatYTick(value: number): string {
  return `${(value / 1000).toFixed(0)}k`;
}
