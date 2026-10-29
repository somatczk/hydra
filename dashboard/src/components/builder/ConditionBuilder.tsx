'use client';

import { Plus } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { ConditionRow } from './ConditionRow';
import type {
  BuilderConditionGroup,
  IndicatorSchema,
  ComparatorSchema,
  ConditionPatch,
} from './types';

interface ConditionBuilderProps {
  group: BuilderConditionGroup;
  indicators: IndicatorSchema[];
  comparators: ComparatorSchema[];
  onAddCondition: () => void;
  onRemoveCondition: (conditionId: string) => void;
  onUpdateCondition: (conditionId: string, patch: ConditionPatch, edit?: boolean) => void;
  onSetOperator: (operator: 'AND' | 'OR') => void;
}

export function ConditionBuilder({
  group,
  indicators,
  comparators,
  onAddCondition,
  onRemoveCondition,
  onUpdateCondition,
  onSetOperator,
}: ConditionBuilderProps) {
  return (
    <div className="rounded-lg border-l-4 border-l-accent border border-line bg-surface p-4">
      {/* Operator toggle */}
      <div className="mb-3 flex items-center gap-2">
        <span className="text-xs font-medium text-ink-2 uppercase">Combine with:</span>
        <div className="flex rounded-lg border border-line overflow-hidden">
          <button
            type="button"
            className={`px-3 py-1 text-xs font-medium transition-colors max-lg:h-11 ${
              group.operator === 'AND'
                ? 'bg-accent text-on-accent'
                : 'bg-surface text-ink-2 hover:bg-hover'
            }`}
            onClick={() => onSetOperator('AND')}
          >
            AND
          </button>
          <button
            type="button"
            className={`px-3 py-1 text-xs font-medium transition-colors max-lg:h-11 ${
              group.operator === 'OR'
                ? 'bg-accent text-on-accent'
                : 'bg-surface text-ink-2 hover:bg-hover'
            }`}
            onClick={() => onSetOperator('OR')}
          >
            OR
          </button>
        </div>
      </div>

      {/* Condition rows */}
      <div className="flex flex-col gap-2">
        {group.conditions.map((condition, index) => (
          <div key={condition.id}>
            {index > 0 && (
              <div className="flex items-center gap-2 py-1">
                <div className="h-px flex-1 bg-line" />
                <span className="text-xs font-medium text-ink-2">{group.operator}</span>
                <div className="h-px flex-1 bg-line" />
              </div>
            )}
            <ConditionRow
              condition={condition}
              indicators={indicators}
              comparators={comparators}
              onUpdate={(patch, edit) => onUpdateCondition(condition.id, patch, edit)}
              onRemove={() => onRemoveCondition(condition.id)}
            />
          </div>
        ))}
      </div>

      {/* Add condition button */}
      <Button
        variant="ghost"
        size="sm"
        onClick={onAddCondition}
        className="mt-3"
      >
        <Plus className="h-4 w-4" />
        Add Condition
      </Button>
    </div>
  );
}
