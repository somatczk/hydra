'use client';

import { useState } from 'react';
import { X } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Select } from '@/components/ui/Select';
import { Input } from '@/components/ui/Input';
import { IndicatorPicker } from './IndicatorPicker';
import { bindingsText, dropBindings, formatIndicatorRef, parseIndicatorRef } from './bindings';
import type {
  BuilderCondition,
  ConditionParams,
  IndicatorSchema,
  ComparatorSchema,
  ConditionPatch,
} from './types';

interface ConditionRowProps {
  condition: BuilderCondition;
  indicators: IndicatorSchema[];
  comparators: ComparatorSchema[];
  /** `edit` marks a user change to a value a tunable may bind (it moves the tunable with it). */
  onUpdate: (patch: ConditionPatch, edit?: boolean) => void;
  onRemove: () => void;
}

/** The registry defaults for an indicator; a param with no default (a period of sma/ema/lag/roc) is left for the user. */
export function defaultParams(schema: IndicatorSchema | undefined): ConditionParams {
  const out: ConditionParams = {};
  for (const p of schema?.params ?? []) if (p.default !== null) out[p.name] = p.default;
  return out;
}

export { formatIndicatorRef, parseIndicatorRef };

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
  const [dropped, setDropped] = useState<string[]>([]);
  /** An edit that changes what one side of the condition is invalidates that side's stored bindings. */
  const dropSide = (side: 'indicator' | 'value'): ConditionPatch => {
    const { kept, dropped: names } = dropBindings(condition.bindings, side);
    if (names.length === 0) return {};
    setDropped((prev) => [...prev, ...names]);
    return { bindings: kept };
  };
  const bound = bindingsText(condition.bindings, condition.params);
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
            onIndicatorChange={(ind) =>
              // the previous indicator's params would make the new one unbuildable (422 at save)
              onUpdate({ indicator: ind, params: defaultParams(indicators.find((i) => i.name === ind)), ...dropSide('indicator') })
            }
            onParamChange={(params) => onUpdate({ params }, true)}
          />
        </div>

        {/* Comparator: takes 4 cols */}
        <div className="md:col-span-4">
          <Select
            label="Comparator"
            options={comparatorOptions}
            value={condition.comparator}
            onChange={(e) => onUpdate({ comparator: e.target.value })}
          />
        </div>

        {/* Value: takes 3 cols */}
        <div className="flex flex-col gap-3 md:col-span-3">
          <Select
            label="Compare to"
            options={COMPARE_TO_OPTIONS}
            value={ref ? 'indicator' : 'number'}
            onChange={(e) => onUpdate({ value: e.target.value === 'indicator' ? 'close' : 0, ...dropSide('value') })}
          />
          {!ref && (
            <Input
              label="Value"
              type="number"
              value={typeof condition.value === 'number' ? condition.value : ''}
              onChange={(e) => {
                const val = parseFloat(e.target.value);
                if (!isNaN(val)) {
                  onUpdate({ value: val }, true);
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
                onUpdate({
                  value: formatIndicatorRef(ind, defaultParams(indicators.find((i) => i.name === ind))),
                  ...dropSide('value'),
                })
              }
              onParamChange={(params) => onUpdate({ value: formatIndicatorRef(ref.name, params) }, true)}
            />
          </div>
        )}

        {(bound || dropped.length > 0) && (
          <div className="flex flex-col gap-1 text-xs text-ink-2 md:col-span-12">
            {bound && <p data-testid="condition-bindings">{bound}</p>}
            {dropped.length > 0 && (
              <p role="status" className="text-caution">
                Dropped {dropped.join(', ')}: {dropped.length === 1 ? 'it' : 'they'} bound what this condition no longer
                uses, so hyperopt and walk-forward can no longer set {dropped.length === 1 ? 'it' : 'them'} here, and a
                tuned value goes with the binding once nothing else binds the name.
              </p>
            )}
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
