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

/** The registry defaults for an indicator; a param with no default (a period of sma/ema/lag/roc) is left for the user. */
export function defaultParams(schema: IndicatorSchema | undefined): ConditionParams {
  const out: ConditionParams = {};
  for (const p of schema?.params ?? []) if (p.default !== null) out[p.name] = p.default;
  return out;
}

/** A condition value naming another indicator, `"name:k=v,k=v"`, as rule_engine.parse_indicator_ref reads it. */
export function parseIndicatorRef(ref: string): { name: string; params: ConditionParams } {
  const [name, args = ''] = ref.split(':');
  const params: ConditionParams = {};
  for (const kv of args.split(',')) {
    const [k, v] = kv.split('=');
    if (k?.trim() && v !== undefined && v.trim() !== '' && Number.isFinite(Number(v))) params[k.trim()] = Number(v);
  }
  return { name, params };
}

export function formatIndicatorRef(name: string, params: ConditionParams): string {
  const args = Object.entries(params).map(([k, v]) => `${k}=${v}`).join(',');
  return args ? `${name}:${args}` : name;
}

const COMPARE_TO_OPTIONS = [
  { value: 'number', label: 'A number' },
  { value: 'indicator', label: 'An indicator' },
];

export function ConditionRow({
  condition,
  indicators,
  comparators,
  onUpdate,
  onRemove,
}: ConditionRowProps) {
  const ref = typeof condition.value === 'string' ? parseIndicatorRef(condition.value) : null;
  const comparatorOptions = comparators.map((c) => ({
    value: c.value,
    label: c.label,
  }));

  return (
    <div className="flex items-start gap-3 rounded-lg border border-line bg-surface p-3">
      <div className="flex-1 grid grid-cols-1 gap-3 md:grid-cols-12">
        {/* Indicator picker: takes 5 cols */}
        <div className="md:col-span-5">
          <IndicatorPicker
            indicators={indicators}
            selectedIndicator={condition.indicator}
            params={condition.params}
            onIndicatorChange={(ind) => {
              onUpdate('indicator', ind);
              // the previous indicator's params would make the new one unbuildable (422 at save)
              onUpdate('params', defaultParams(indicators.find((i) => i.name === ind)));
            }}
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
        <div className="flex flex-col gap-3 md:col-span-3">
          <Select
            label="Compare to"
            options={COMPARE_TO_OPTIONS}
            value={ref ? 'indicator' : 'number'}
            onChange={(e) => onUpdate('value', e.target.value === 'indicator' ? 'close' : 0)}
          />
          {!ref && (
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
          )}
        </div>

        {ref && (
          <div className="md:col-span-12">
            <IndicatorPicker
              label="Compared indicator"
              indicators={indicators}
              selectedIndicator={ref.name}
              params={ref.params}
              onIndicatorChange={(ind) =>
                onUpdate('value', formatIndicatorRef(ind, defaultParams(indicators.find((i) => i.name === ind))))
              }
              onParamChange={(params) => onUpdate('value', formatIndicatorRef(ref.name, params))}
            />
          </div>
        )}
      </div>

      {/* Remove button */}
      <Button
        variant="ghost"
        size="sm"
        onClick={onRemove}
        className="mt-6 shrink-0 max-lg:h-11 max-lg:w-11"
        aria-label="Remove condition"
      >
        <X className="h-4 w-4" />
      </Button>
    </div>
  );
}
