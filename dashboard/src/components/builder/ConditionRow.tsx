'use client';

import { X } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Select } from '@/components/ui/Select';
import { Input } from '@/components/ui/Input';
import { IndicatorPicker } from './IndicatorPicker';
import type {
  BuilderCondition,
  ConditionParams,
  IndicatorSchema,
  ComparatorSchema,
} from './types';

interface ConditionRowProps {
  condition: BuilderCondition;
  indicators: IndicatorSchema[];
  comparators: ComparatorSchema[];
  onUpdate: (field: keyof BuilderCondition, value: string | number | ConditionParams) => void;
  onRemove: () => void;
}

export function ConditionRow({
  condition,
  indicators,
  comparators,
  onUpdate,
  onRemove,
}: ConditionRowProps) {
  const comparatorOptions = comparators.map((c) => ({
    value: c.value,
    label: c.label,
  }));

  return (
    <div className="flex items-start gap-3 rounded-lg border border-border-default bg-bg-primary p-3">
      <div className="flex-1 grid grid-cols-1 gap-3 md:grid-cols-12">
        {/* Indicator picker: takes 5 cols */}
        <div className="md:col-span-5">
          <IndicatorPicker
            indicators={indicators}
            selectedIndicator={condition.indicator}
            params={condition.params}
            onIndicatorChange={(ind) => onUpdate('indicator', ind)}
            onParamChange={(params) => onUpdate('params', params)}
          />
        </div>

        {/* Comparator: takes 4 cols */}
        <div className="md:col-span-4">
          <Select
            label="Comparator"
            options={comparatorOptions}
            value={condition.comparator}
            onChange={(e) => onUpdate('comparator', e.target.value)}
          />
        </div>

        {/* Value: takes 3 cols */}
        <div className="md:col-span-3">
          <Input
            label="Value"
            type="number"
            value={typeof condition.value === 'number' ? condition.value : ''}
            onChange={(e) => {
              const val = parseFloat(e.target.value);
              if (!isNaN(val)) {
                onUpdate('value', val);
              }
            }}
            placeholder="0"
          />
        </div>
      </div>

      {/* Remove button */}
      <Button
        variant="ghost"
        size="sm"
        onClick={onRemove}
        className="mt-6 shrink-0"
        aria-label="Remove condition"
      >
        <X className="h-4 w-4" />
      </Button>
    </div>
  );
}
