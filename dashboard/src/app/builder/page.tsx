'use client';

import { useReducer, useEffect, useState, useCallback, Suspense } from 'react';
import { Save, Plus } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { RuleSection } from '@/components/builder/RuleSection';
import { TimeframeSelector } from '@/components/builder/TimeframeSelector';
import { RiskConfigurator } from '@/components/builder/RiskConfigurator';
import { SignalPreview } from '@/components/builder/SignalPreview';
import { StrategyNameDialog } from '@/components/builder/StrategyNameDialog';
import { useSearchParams } from 'next/navigation';
import { useToast } from '@/components/ui/Toast';
import { fetchApi } from '@/lib/api';
import type {
  BuilderState,
  BuilderAction,
  BuilderCondition,
  BuilderConditionGroup,
  IndicatorSchema,
  ComparatorSchema,
  ConditionParams,
  StopLossConfig,
  TakeProfitConfig,
  SizingConfig,
  TimeframeConfig,
  RuleSection as RuleSectionKey,
  StrategyDetail,
  SerializedConditionGroup,
} from '@/components/builder/types';

/* ---------- Initial state ---------- */

function emptyGroup(): BuilderConditionGroup {
  return { operator: 'AND', conditions: [] };
}

const initialState: BuilderState = {
  entryLong: emptyGroup(),
  exitLong: emptyGroup(),
  entryShort: emptyGroup(),
  exitShort: emptyGroup(),
  timeframes: {
    primary: '1h',
    confirmation: undefined,
    entry: undefined,
  },
  risk: {
    stopLoss: { method: 'atr', value: 2.0 },
    takeProfit: { method: 'atr', value: 3.0 },
    sizing: {
      method: 'fixed_fractional',
      riskPerTradePct: 1.0,
      maxPositionPct: 10.0,
    },
  },
  editingId: null,
  strategyName: '',
  strategyDescription: '',
};

/* ---------- ID generation ---------- */

let conditionCounter = 0;
function newConditionId(): string {
  conditionCounter += 1;
  return `cond_${conditionCounter}_${Date.now()}`;
}

/* ---------- Helpers ---------- */

function apiGroupToBuilder(
  group: SerializedConditionGroup | null,
): BuilderConditionGroup {
  if (!group) return emptyGroup();
  return {
    operator: group.operator as 'AND' | 'OR',
    conditions: group.conditions.map((c) => ({
      id: crypto.randomUUID(),
      indicator: c.indicator,
      params: c.params,
      comparator: c.comparator,
      value: c.value,
    })),
  };
}

/* ---------- Reducer ---------- */

function builderReducer(state: BuilderState, action: BuilderAction): BuilderState {
  switch (action.type) {
    case 'ADD_CONDITION': {
      const section = action.section;
      const newCondition: BuilderCondition = {
        id: newConditionId(),
        indicator: 'rsi',
        params: { period: 14 },
        comparator: 'less_than',
        value: 30,
      };
      return {
        ...state,
        [section]: {
          ...state[section],
          conditions: [...state[section].conditions, newCondition],
        },
      };
    }
    case 'REMOVE_CONDITION': {
      const section = action.section;
      return {
        ...state,
        [section]: {
          ...state[section],
          conditions: state[section].conditions.filter(
            (c: BuilderCondition) => c.id !== action.conditionId,
          ),
        },
      };
    }
    case 'UPDATE_CONDITION': {
      const section = action.section;
      return {
        ...state,
        [section]: {
          ...state[section],
          conditions: state[section].conditions.map((c: BuilderCondition) =>
            c.id === action.conditionId
              ? { ...c, [action.field]: action.value }
              : c,
          ),
        },
      };
    }
    case 'SET_OPERATOR': {
      const section = action.section;
      return {
        ...state,
        [section]: {
          ...state[section],
          operator: action.operator,
        },
      };
    }
    case 'SET_TIMEFRAME': {
      const value = action.value || undefined;
      return {
        ...state,
        timeframes: {
          ...state.timeframes,
          [action.field]: value,
        },
      };
    }
    case 'SET_STOP_LOSS':
      return {
        ...state,
        risk: {
          ...state.risk,
          stopLoss: { ...state.risk.stopLoss, ...action.stopLoss },
        },
      };
    case 'SET_TAKE_PROFIT':
      return {
        ...state,
        risk: {
          ...state.risk,
          takeProfit: { ...state.risk.takeProfit, ...action.takeProfit },
        },
      };
    case 'SET_SIZING':
      return {
        ...state,
        risk: {
          ...state.risk,
          sizing: { ...state.risk.sizing, ...action.sizing },
        },
      };
    case 'LOAD_STRATEGY': {
      const { payload } = action;
      return {
        entryLong: apiGroupToBuilder(payload.rules.entry_long),
        exitLong: apiGroupToBuilder(payload.rules.exit_long),
        entryShort: apiGroupToBuilder(payload.rules.entry_short),
        exitShort: apiGroupToBuilder(payload.rules.exit_short),
        timeframes: {
          primary: payload.timeframes.primary,
          confirmation: payload.timeframes.confirmation || undefined,
          entry: payload.timeframes.entry || undefined,
        },
        risk: {
          stopLoss: {
            method: (payload.risk.stop_loss_method as 'atr' | 'fixed_pct') || 'atr',
            value: payload.risk.stop_loss_value ?? 2.0,
          },
          takeProfit: {
            method: (payload.risk.take_profit_method as 'atr' | 'fixed_pct') || 'atr',
            value: payload.risk.take_profit_value ?? 3.0,
          },
          sizing: {
            method:
              (payload.risk.sizing_method as SizingConfig['method']) || 'fixed_fractional',
            riskPerTradePct: payload.risk.sizing_params?.risk_per_trade_pct ?? 1.0,
            maxPositionPct: payload.risk.sizing_params?.max_position_pct ?? 10.0,
          },
        },
        editingId: payload.id,
        strategyName: payload.name,
        strategyDescription: payload.description,
      };
    }
    case 'RESET':
      return initialState;
    default:
      return state;
  }
}

/* ---------- Rule section configs ---------- */

interface RuleSectionConfig {
  key: RuleSectionKey;
  title: string;
  description: string;
  colorClass: string;
}

const RULE_SECTIONS: RuleSectionConfig[] = [
  {
    key: 'entryLong',
    title: 'Entry Long',
    description: 'Conditions for opening a long (buy) position',
    colorClass: 'bg-status-success',
  },
  {
    key: 'exitLong',
    title: 'Exit Long',
    description: 'Conditions for closing a long position',
    colorClass: 'bg-status-warning',
  },
  {
    key: 'entryShort',
    title: 'Entry Short',
    description: 'Conditions for opening a short (sell) position',
    colorClass: 'bg-status-error',
  },
  {
    key: 'exitShort',
    title: 'Exit Short',
    description: 'Conditions for closing a short position',
    colorClass: 'bg-status-info',
  },
];

/* ---------- Default indicator/comparator data ---------- */

const DEFAULT_INDICATORS: IndicatorSchema[] = [
  {
    name: 'rsi',
    category: 'momentum',
    description: 'Relative Strength Index (0-100 oscillator)',
    params: [{ name: 'period', type: 'int', default: 14, min: 2, max: 200 }],
  },
  {
    name: 'sma',
    category: 'trend',
    description: 'Simple Moving Average',
    params: [{ name: 'period', type: 'int', default: null, min: 2, max: 200 }],
  },
  {
    name: 'ema',
    category: 'trend',
    description: 'Exponential Moving Average',
    params: [{ name: 'period', type: 'int', default: null, min: 2, max: 200 }],
  },
  {
    name: 'macd',
    category: 'trend',
    description: 'Moving Average Convergence Divergence',
    params: [
      { name: 'fast', type: 'int', default: 12, min: 2, max: 100 },
      { name: 'slow', type: 'int', default: 26, min: 2, max: 200 },
      { name: 'signal', type: 'int', default: 9, min: 2, max: 100 },
    ],
  },
  {
    name: 'bollinger_bands',
    category: 'volatility',
    description: 'Bollinger Bands (upper, middle, lower)',
    params: [
      { name: 'period', type: 'int', default: 20, min: 2, max: 200 },
      { name: 'std_dev', type: 'float', default: 2.0, min: 0.1, max: 5.0 },
    ],
  },
  {
    name: 'atr',
    category: 'volatility',
    description: 'Average True Range (volatility measure)',
    params: [{ name: 'period', type: 'int', default: 14, min: 2, max: 200 }],
  },
];

const DEFAULT_COMPARATORS: ComparatorSchema[] = [
  { value: 'less_than', label: 'Less Than', description: 'Value is less than target' },
  { value: 'greater_than', label: 'Greater Than', description: 'Value is greater than target' },
  { value: 'crosses_above', label: 'Crosses Above', description: 'Crosses from below to above' },
  { value: 'crosses_below', label: 'Crosses Below', description: 'Crosses from above to below' },
  { value: 'equals', label: 'Equals', description: 'Value equals target' },
];

/* ---------- Page ---------- */

function BuilderPageContent() {
  const [state, dispatch] = useReducer(builderReducer, initialState);
  const [indicators, setIndicators] = useState<IndicatorSchema[]>(DEFAULT_INDICATORS);
  const [comparators, setComparators] = useState<ComparatorSchema[]>(DEFAULT_COMPARATORS);
  const [saveDialogOpen, setSaveDialogOpen] = useState(false);
  const searchParams = useSearchParams();
  const { toast } = useToast();

  /* Fetch indicators and comparators from API */
  useEffect(() => {
    const fetchData = async () => {
      try {
        const [indData, compData] = await Promise.all([
          fetchApi<IndicatorSchema[]>('/api/builder/indicators').catch(() => null),
          fetchApi<ComparatorSchema[]>('/api/builder/comparators').catch(() => null),
        ]);
        if (indData && indData.length > 0) setIndicators(indData);
        if (compData && compData.length > 0) setComparators(compData);
      } catch {
        // Use default data if API is not available
      }
    };
    fetchData();
  }, []);

  /* Load strategy from URL param ?strategy={id} */
  useEffect(() => {
    const strategyId = searchParams.get('strategy');
    if (!strategyId) return;
    fetchApi<StrategyDetail>(`/api/builder/strategies/${strategyId}`)
      .then((detail) => dispatch({ type: 'LOAD_STRATEGY', payload: detail }))
      .catch(() => { /* strategy not found */ });
  }, [searchParams]);

  const handleSaved = useCallback(
    (name: string) => {
      toast('success', `Strategy "${name}" saved successfully`);
    },
    [toast],
  );

  return (
    <div className="flex flex-col gap-6">
      {/* Page header */}
      <div>
        <h2 className="text-xl font-bold text-text-primary font-display">Strategy Builder</h2>
        <p className="text-sm text-text-muted mt-1">
          Build trading strategies visually using indicators and conditions
        </p>
      </div>

      {/* Rule sections */}
      <div className="flex flex-col gap-4">
        {RULE_SECTIONS.map((section) => (
          <RuleSection
            key={section.key}
            title={section.title}
            description={section.description}
            group={state[section.key]}
            indicators={indicators}
            comparators={comparators}
            colorClass={section.colorClass}
            onAddCondition={() =>
              dispatch({ type: 'ADD_CONDITION', section: section.key })
            }
            onRemoveCondition={(conditionId: string) =>
              dispatch({
                type: 'REMOVE_CONDITION',
                section: section.key,
                conditionId,
              })
            }
            onUpdateCondition={(
              conditionId: string,
              field: keyof BuilderCondition,
              value: string | number | ConditionParams,
            ) =>
              dispatch({
                type: 'UPDATE_CONDITION',
                section: section.key,
                conditionId,
                field,
                value,
              })
            }
            onSetOperator={(operator: 'AND' | 'OR') =>
              dispatch({ type: 'SET_OPERATOR', section: section.key, operator })
            }
          />
        ))}
      </div>

      {/* Timeframe selector */}
      <TimeframeSelector
        timeframes={state.timeframes}
        onChange={(field: keyof TimeframeConfig, value: string) =>
          dispatch({ type: 'SET_TIMEFRAME', field, value })
        }
      />

      {/* Risk configurator */}
      <RiskConfigurator
        risk={state.risk}
        onStopLossChange={(stopLoss: Partial<StopLossConfig>) =>
          dispatch({ type: 'SET_STOP_LOSS', stopLoss })
        }
        onTakeProfitChange={(takeProfit: Partial<TakeProfitConfig>) =>
          dispatch({ type: 'SET_TAKE_PROFIT', takeProfit })
        }
        onSizingChange={(sizing: Partial<SizingConfig>) =>
          dispatch({ type: 'SET_SIZING', sizing })
        }
      />

      {/* Signal preview */}
      <SignalPreview state={state} />

      {/* Action buttons */}
      <div className="flex justify-end gap-3">
        {state.editingId && (
          <Button
            variant="outline"
            size="lg"
            onClick={() => dispatch({ type: 'RESET' })}
          >
            <Plus className="h-4 w-4" />
            New Strategy
          </Button>
        )}
        <Button
          variant="primary"
          size="lg"
          onClick={() => setSaveDialogOpen(true)}
        >
          <Save className="h-4 w-4" />
          {state.editingId ? 'Update Strategy' : 'Save Strategy'}
        </Button>
      </div>

      {/* Save dialog */}
      <StrategyNameDialog
        open={saveDialogOpen}
        onClose={() => setSaveDialogOpen(false)}
        state={state}
        onSaved={handleSaved}
        editingId={state.editingId}
        initialName={state.strategyName}
        initialDescription={state.strategyDescription}
      />
    </div>
  );
}

export default function BuilderPage() {
  return (
    <Suspense>
      <BuilderPageContent />
    </Suspense>
  );
}
