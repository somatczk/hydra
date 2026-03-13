'use client';

import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { ConditionBuilder } from './ConditionBuilder';
import type {
  BuilderCondition,
  BuilderConditionGroup,
  ConditionParams,
  IndicatorSchema,
  ComparatorSchema,
} from './types';

interface RuleSectionProps {
  title: string;
  description: string;
  group: BuilderConditionGroup;
  indicators: IndicatorSchema[];
  comparators: ComparatorSchema[];
  colorClass: string;
  onAddCondition: () => void;
  onRemoveCondition: (conditionId: string) => void;
  onUpdateCondition: (
    conditionId: string,
    field: keyof BuilderCondition,
    value: string | number | ConditionParams,
  ) => void;
  onSetOperator: (operator: 'AND' | 'OR') => void;
}

export function RuleSection({
  title,
  description,
  group,
  indicators,
  comparators,
  colorClass,
  onAddCondition,
  onRemoveCondition,
  onUpdateCondition,
  onSetOperator,
}: RuleSectionProps) {
  const [isOpen, setIsOpen] = useState(true);
  const conditionCount = group.conditions.length;

  return (
    <div className="rounded-xl border border-border-default bg-bg-elevated overflow-hidden">
      {/* Header - always visible */}
      <button
        type="button"
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-bg-hover transition-colors"
        onClick={() => setIsOpen(!isOpen)}
        aria-expanded={isOpen}
      >
        <div className={`h-3 w-3 rounded-full ${colorClass}`} />
        {isOpen ? (
          <ChevronDown className="h-4 w-4 text-text-muted" />
        ) : (
          <ChevronRight className="h-4 w-4 text-text-muted" />
        )}
        <div className="flex-1">
          <h4 className="text-sm font-semibold text-text-primary">{title}</h4>
          <p className="text-xs text-text-muted">{description}</p>
        </div>
        <span className="rounded-full bg-bg-tertiary px-2 py-0.5 text-xs font-medium text-text-muted">
          {conditionCount} {conditionCount === 1 ? 'condition' : 'conditions'}
        </span>
      </button>

      {/* Collapsible body */}
      {isOpen && (
        <div className="border-t border-border-default px-4 py-4">
          <ConditionBuilder
            group={group}
            indicators={indicators}
            comparators={comparators}
            onAddCondition={onAddCondition}
            onRemoveCondition={onRemoveCondition}
            onUpdateCondition={onUpdateCondition}
            onSetOperator={onSetOperator}
          />
        </div>
      )}
    </div>
  );
}
