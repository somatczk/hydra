export type TimeResolution = 'hours' | 'days' | 'months';

export interface EquityPoint {
  timestamp: string;
  value: number;
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
  return `$${(value / 1000).toFixed(0)}k`;
}

export function makeCustomTooltip(resolution: TimeResolution) {
  return function CustomTooltip({ active, payload, label }: { active?: boolean; payload?: Array<{ value: number }>; label?: string }) {
    if (!active || !payload?.length) return null;
    return (
      <div className="rounded-lg border border-border-default bg-bg-elevated px-3 py-2 shadow-lg">
        <p className="text-xs text-text-muted">{label ? formatXTick(label, resolution) : ''}</p>
        <p className="text-sm font-medium text-text-primary">
          ${payload[0].value.toLocaleString('en-US', { minimumFractionDigits: 2 })}
        </p>
      </div>
    );
  };
}
