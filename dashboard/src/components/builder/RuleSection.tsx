'use client';

import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { ConditionBuilder } from './ConditionBuilder';
import type {
  BuilderConditionGroup,
  IndicatorSchema,
  ComparatorSchema,
  ConditionPatch,
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
  onUpdateCondition: (conditionId: string, patch: ConditionPatch, edit?: boolean) => void;
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
    <div className="rounded-lg border border-line bg-raised overflow-hidden">
      {/* Header - always visible */}
      <button
        type="button"
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-hover transition-colors"
        onClick={() => setIsOpen(!isOpen)}
        aria-expanded={isOpen}
      >
        <div className={`h-3 w-3 rounded-full ${colorClass}`} />
        {isOpen ? (
          <ChevronDown className="h-4 w-4 text-ink-2" />
        ) : (
          <ChevronRight className="h-4 w-4 text-ink-2" />
        )}
        <div className="flex-1">
          <h2 className="text-sm font-semibold text-ink-1">{title}</h2>
          <p className="text-xs text-ink-2">{description}</p>
        </div>
        <span className="rounded-full bg-surface px-2 py-0.5 text-xs font-medium text-ink-2">
          {conditionCount} {conditionCount === 1 ? 'condition' : 'conditions'}
        </span>
      </button>

      {/* Collapsible body */}
      {isOpen && (
        <div className="border-t border-line px-4 py-4">
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
