'use client';

import { Select } from '@/components/ui/Select';
import { DataCard } from '@/components/ui/DataCard';
import type { TimeframeConfig } from './types';

interface TimeframeSelectorProps {
  timeframes: TimeframeConfig;
  onChange: (field: keyof TimeframeConfig, value: string) => void;
}

const TIMEFRAME_OPTIONS = [
  { value: '1m', label: '1 Minute' },
  { value: '5m', label: '5 Minutes' },
  { value: '15m', label: '15 Minutes' },
  { value: '1h', label: '1 Hour' },
  { value: '4h', label: '4 Hours' },
  { value: '1d', label: '1 Day' },
];

const OPTIONAL_TIMEFRAME_OPTIONS = [
  { value: '', label: 'None' },
  ...TIMEFRAME_OPTIONS,
];

export function TimeframeSelector({ timeframes, onChange }: TimeframeSelectorProps) {
  return (
    <DataCard title="Timeframes" description="Select candlestick timeframes for analysis">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Select
          label="Primary Timeframe"
          options={TIMEFRAME_OPTIONS}
          value={timeframes.primary}
          onChange={(e) => onChange('primary', e.target.value)}
        />
        <Select
          label="Confirmation Timeframe"
          options={OPTIONAL_TIMEFRAME_OPTIONS}
          value={timeframes.confirmation ?? ''}
          onChange={(e) => onChange('confirmation', e.target.value)}
          hint="Optional higher timeframe for trend confirmation"
        />
        <Select
          label="Entry Timeframe"
          options={OPTIONAL_TIMEFRAME_OPTIONS}
          value={timeframes.entry ?? ''}
          onChange={(e) => onChange('entry', e.target.value)}
          hint="Optional lower timeframe for precise entries"
        />
      </div>
    </DataCard>
  );
}
