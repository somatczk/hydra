'use client';

import { useMemo } from 'react';
import { Select } from '@/components/ui/Select';
import { Input } from '@/components/ui/Input';
import type { IndicatorSchema, ConditionParams } from './types';

interface IndicatorPickerProps {
  indicators: IndicatorSchema[];
  selectedIndicator: string;
  params: ConditionParams;
  onIndicatorChange: (indicator: string) => void;
  onParamChange: (params: ConditionParams) => void;
}

const CATEGORY_LABELS: Record<string, string> = {
  trend: 'Trend',
  momentum: 'Momentum',
  volatility: 'Volatility',
  volume: 'Volume',
  other: 'Other',
};

const CATEGORY_ORDER = ['trend', 'momentum', 'volatility', 'volume', 'other'];

export function IndicatorPicker({
  indicators,
  selectedIndicator,
  params,
  onIndicatorChange,
  onParamChange,
}: IndicatorPickerProps) {
  const groupedOptions = useMemo(() => {
    const groups: Record<string, { value: string; label: string }[]> = {};
    for (const ind of indicators) {
      const cat = ind.category;
      if (!groups[cat]) {
        groups[cat] = [];
      }
      groups[cat].push({
        value: ind.name,
        label: `${ind.name.toUpperCase()} - ${ind.description}`,
      });
    }
    // Flatten into options with category prefixes
    const opts: { value: string; label: string }[] = [];
    for (const cat of CATEGORY_ORDER) {
      const items = groups[cat];
      if (items && items.length > 0) {
        for (const item of items) {
          opts.push({
            value: item.value,
            label: `[${CATEGORY_LABELS[cat]}] ${item.label}`,
          });
        }
      }
    }
    return opts;
  }, [indicators]);

  const selectedSchema = useMemo(
    () => indicators.find((ind) => ind.name === selectedIndicator),
    [indicators, selectedIndicator],
  );

  const handleParamChange = (paramName: string, value: string) => {
    const numVal = parseFloat(value);
    if (!isNaN(numVal)) {
      onParamChange({ ...params, [paramName]: numVal });
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <Select
        label="Indicator"
        options={groupedOptions}
        value={selectedIndicator}
        onChange={(e) => onIndicatorChange(e.target.value)}
        placeholder="Select indicator..."
      />
      {selectedSchema && selectedSchema.params.length > 0 && (
        <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
          {selectedSchema.params.map((param) => (
            <Input
              key={param.name}
              label={param.name}
              type="number"
              value={params[param.name] ?? param.default ?? ''}
              min={param.min ?? undefined}
              max={param.max ?? undefined}
              placeholder={param.default !== null ? String(param.default) : ''}
              onChange={(e) => handleParamChange(param.name, e.target.value)}
              hint={
                param.min !== null && param.max !== null
                  ? `${param.min} - ${param.max}`
                  : undefined
              }
            />
          ))}
        </div>
      )}
    </div>
  );
}
