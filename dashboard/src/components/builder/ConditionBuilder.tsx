'use client';

import { Plus } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { ConditionRow } from './ConditionRow';
import type {
  BuilderCondition,
  BuilderConditionGroup,
  ConditionParams,
  IndicatorSchema,
  ComparatorSchema,
} from './types';

interface ConditionBuilderProps {
  group: BuilderConditionGroup;
  indicators: IndicatorSchema[];
  comparators: ComparatorSchema[];
  onAddCondition: () => void;
  onRemoveCondition: (conditionId: string) => void;
  onUpdateCondition: (
    conditionId: string,
    field: keyof BuilderCondition,
    value: string | number | ConditionParams,
  ) => void;
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
    <div className="rounded-lg border-l-4 border-l-accent-primary border border-border-default bg-bg-secondary p-4">
      {/* Operator toggle */}
      <div className="mb-3 flex items-center gap-2">
        <span className="text-xs font-medium text-text-muted uppercase">Combine with:</span>
        <div className="flex rounded-lg border border-border-default overflow-hidden">
          <button
            type="button"
            className={`px-3 py-1 text-xs font-medium transition-colors ${
              group.operator === 'AND'
                ? 'bg-accent-primary text-white'
                : 'bg-bg-primary text-text-secondary hover:bg-bg-hover'
            }`}
            onClick={() => onSetOperator('AND')}
          >
            AND
          </button>
          <button
            type="button"
            className={`px-3 py-1 text-xs font-medium transition-colors ${
              group.operator === 'OR'
                ? 'bg-accent-primary text-white'
                : 'bg-bg-primary text-text-secondary hover:bg-bg-hover'
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
                <div className="h-px flex-1 bg-border-default" />
                <span className="text-xs font-medium text-text-muted">{group.operator}</span>
                <div className="h-px flex-1 bg-border-default" />
              </div>
            )}
            <ConditionRow
              condition={condition}
              indicators={indicators}
              comparators={comparators}
              onUpdate={(field, value) => onUpdateCondition(condition.id, field, value)}
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
