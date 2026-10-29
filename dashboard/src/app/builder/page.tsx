'use client';

import { useReducer, useEffect, useState, useCallback, useRef, Suspense } from 'react';
import { Save, Plus, FlaskConical } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { RuleSection } from '@/components/builder/RuleSection';
import { TimeframeSelector } from '@/components/builder/TimeframeSelector';
import { RiskConfigurator } from '@/components/builder/RiskConfigurator';
import { SignalPreview } from '@/components/builder/SignalPreview';
import { StrategyNameDialog } from '@/components/builder/StrategyNameDialog';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { useSearchParams, useRouter } from 'next/navigation';
import { useToast } from '@/components/ui/Toast';
import { fetchApi } from '@/lib/api';
import { describeApiError } from '@/lib/apiState';
import { DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { logger } from '@/lib/logger';
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

const initialDraft: Omit<BuilderState, 'baseline'> = {
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
  mlOverlay: null,
  editingId: null,
  strategyName: '',
  strategyDescription: '',
  exchangeId: 'binance',
  symbol: 'BTCUSDT',
  leverage: 1,
};

function snapshot(state: BuilderState): string {
  return JSON.stringify({ ...state, baseline: undefined });
}

const initialState: BuilderState = { ...initialDraft, baseline: JSON.stringify(initialDraft) };

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
    case 'SET_ML_OVERLAY':
      return { ...state, mlOverlay: action.mlOverlay };
    case 'LOAD_STRATEGY': {
      const { payload } = action;
      const next: BuilderState = {
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
            method: (payload.risk.stop_loss_method as StopLossConfig['method']) || 'atr',
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
        mlOverlay: payload.ml_overlay
          ? { modelName: payload.ml_overlay.model_name, confidenceThreshold: payload.ml_overlay.confidence_threshold }
          : null,
        editingId: payload.id,
        strategyName: payload.name,
        strategyDescription: payload.description,
        exchangeId: payload.exchange_id || 'binance',
        symbol: payload.symbol || 'BTCUSDT',
        leverage: payload.leverage ?? 1,
        baseline: '',
      };
      return { ...next, baseline: snapshot(next) };
    }
    case 'MARK_CLEAN':
      return { ...state, baseline: snapshot(state) };
    case 'RESET':
      return { ...initialState, baseline: snapshot(initialState) };
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
    colorClass: 'bg-ink-2',
  },
  {
    key: 'exitLong',
    title: 'Exit Long',
    description: 'Conditions for closing a long position',
    colorClass: 'bg-caution',
  },
  {
    key: 'entryShort',
    title: 'Entry Short',
    description: 'Conditions for opening a short (sell) position',
    colorClass: 'bg-danger',
  },
  {
    key: 'exitShort',
    title: 'Exit Short',
    description: 'Conditions for closing a short position',
    colorClass: 'bg-ink-2',
  },
];

/* ---------- Offline fallback: a subset of GET /api/strategies/indicators, rule-usable names only ---------- */

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
    description: 'MACD line (fast EMA - slow EMA)',
    params: [
      { name: 'fast', type: 'int', default: 12, min: 2, max: 100 },
      { name: 'slow', type: 'int', default: 26, min: 2, max: 200 },
      { name: 'signal', type: 'int', default: 9, min: 2, max: 100 },
    ],
  },
  {
    name: 'bollinger_upper',
    category: 'volatility',
    description: 'Upper Bollinger Band',
    params: [
      { name: 'period', type: 'int', default: 20, min: 2, max: 200 },
      { name: 'std_dev', type: 'float', default: 2.0, min: 0.1, max: 5.0 },
    ],
  },
  {
    name: 'bollinger_lower',
    category: 'volatility',
    description: 'Lower Bollinger Band',
    params: [
      { name: 'period', type: 'int', default: 20, min: 2, max: 200 },
      { name: 'std_dev', type: 'float', default: 2.0, min: 0.1, max: 5.0 },
    ],
  },
  {
    name: 'atr',
    category: 'volatility',
    description: 'Average True Range (Wilder)',
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
  const [loadError, setLoadError] = useState<unknown>(null);
  const [discardOpen, setDiscardOpen] = useState(false);
  /** Held navigation while the discard confirm is open: an intercepted click href or a trapped traversal; null when opened by New Strategy. */
  const [pendingNav, setPendingNav] = useState<string | null>(null);
  const searchParams = useSearchParams();
  const router = useRouter();
  const { toast } = useToast();
  const saveAndBacktestRef = useRef(false);

  const dirty = snapshot(state) !== state.baseline;
  /* Dirty read at effect-run time, not render time: the ?strategy load must never fire over a dirty draft. */
  const dirtyRef = useRef(dirty);
  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  /* S1: while dirty, nav-link clicks and history traversals (back/forward, phone back) are stopped and routed through the discard confirm. */
  useEffect(() => {
    if (!dirty) return;
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const anchor = (e.target as HTMLElement | null)?.closest?.('a[href]') as HTMLAnchorElement | null;
      if (!anchor || (anchor.target && anchor.target !== '_self')) return;
      const url = new URL(anchor.getAttribute('href') ?? '', window.location.href);
      const sameRoute = url.pathname === window.location.pathname && url.search === window.location.search;
      if (url.origin !== window.location.origin || sameRoute) return;
      e.preventDefault();
      e.stopPropagation();
      setPendingNav(url.pathname + url.search + url.hash);
      setDiscardOpen(true);
    };
    document.addEventListener('click', onClick, true);
    const restoreHref = window.location.href;
    const restoreState = window.history.state;
    const onPopState = () => {
      const target = new URL(window.location.href);
      const restore = new URL(restoreHref);
      if (target.pathname === restore.pathname && target.search === restore.search) return;
      history.pushState(restoreState, '', restoreHref);
      // A re-fired popstate with the restored entry's state supersedes the router's in-flight traversal.
      window.dispatchEvent(new PopStateEvent('popstate', { state: history.state }));
      setPendingNav(target.pathname + target.search + target.hash);
      setDiscardOpen(true);
    };
    window.addEventListener('popstate', onPopState, true);
    return () => {
      document.removeEventListener('click', onClick, true);
      window.removeEventListener('popstate', onPopState, true);
    };
  }, [dirty]);

  /* Fetch indicators and comparators from API */
  useEffect(() => {
    const fetchData = async () => {
      try {
        const [indResult, compResult] = await Promise.allSettled([
          fetchApi<IndicatorSchema[]>('/api/strategies/indicators'),
          fetchApi<ComparatorSchema[]>('/api/strategies/comparators'),
        ]);
        if (indResult.status === 'fulfilled' && indResult.value.length > 0) setIndicators(indResult.value);
        if (compResult.status === 'fulfilled' && compResult.value.length > 0) setComparators(compResult.value);
        const failed = [
          indResult.status === 'rejected' ? ['indicators', indResult.reason] : null,
          compResult.status === 'rejected' ? ['comparators', compResult.reason] : null,
        ].filter((f): f is [string, unknown] => f !== null);
        if (failed.length > 0) {
          const detail = failed.map(([what, err]) => `${what}: ${describeApiError(err, 'unavailable')}`).join('; ');
          logger.warn('Builder', 'Builder catalogue unavailable', detail);
          toast('warning', `Using built-in indicator list; server catalogue unavailable (${detail})`);
        }
      } catch (err) {
        logger.warn('Builder', 'Failed to fetch indicators/comparators', err);
      }
    };
    fetchData();
  }, [toast]);

  /* Load strategy from URL param ?strategy={id} */
  useEffect(() => {
    const strategyId = searchParams.get('strategy');
    if (!strategyId || dirtyRef.current) return;
    setLoadError(null);
    fetchApi<StrategyDetail>(`/api/strategies/${strategyId}`)
      .then((detail) => {
        if (dirtyRef.current) return;
        dispatch({ type: 'LOAD_STRATEGY', payload: detail });
        logger.info('Builder', `Loaded strategy "${detail.name}"`);
        toast('success', `Loaded strategy "${detail.name}"`);
      })
      .catch((err) => {
        logger.error('Builder', 'Failed to load strategy', err);
        setLoadError(err);
        toast('error', `Failed to load strategy: ${describeApiError(err, 'not found')}`);
      });
  }, [searchParams, toast]);

  const handleSaved = useCallback(
    (name: string, id: string) => {
      logger.info('Builder', `Strategy "${name}" saved`);
      dispatch({ type: 'MARK_CLEAN' });
      toast('success', `Strategy "${name}" saved successfully`);
      if (saveAndBacktestRef.current) {
        saveAndBacktestRef.current = false;
        router.push(`/backtest?strategy=${id}&autorun=true`);
      }
    },
    [toast, router],
  );

  const handleSaveAndBacktest = useCallback(() => {
    saveAndBacktestRef.current = true;
    setSaveDialogOpen(true);
  }, []);

  const openDiscard = () => {
    setPendingNav(null);
    setDiscardOpen(true);
  };

  const confirmDiscard = async () => {
    setDiscardOpen(false);
    const href = pendingNav;
    setPendingNav(null);
    dispatch({ type: 'RESET' });
    if (href) router.push(href);
  };

  return (
    <div className="flex flex-col gap-6">
      {/* Page header */}
      <div>
        <h1 className="text-xl font-semibold text-ink-1">Strategy builder</h1>
        <p className="text-sm text-ink-2 mt-1">
          Build trading strategies visually using indicators and conditions
        </p>
      </div>

      {loadError !== null && (
        <div data-testid="load-error">
          <DataStateErrorCard error={loadError} fallback="Failed to load strategy" />
        </div>
      )}

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
            onAddCondition={() => dispatch({ type: 'ADD_CONDITION', section: section.key })}
            onRemoveCondition={(conditionId: string) =>
              dispatch({ type: 'REMOVE_CONDITION', section: section.key, conditionId })
            }
            onUpdateCondition={(
              conditionId: string,
              field: keyof BuilderCondition,
              value: string | number | ConditionParams,
            ) => dispatch({ type: 'UPDATE_CONDITION', section: section.key, conditionId, field, value })}
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
      <div className="flex flex-wrap justify-end gap-3">
        {state.editingId && (
          <Button
            variant="outline"
            size="lg"
            onClick={dirty ? openDiscard : () => dispatch({ type: 'RESET' })}
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
          {state.editingId ? 'Update strategy' : 'Save strategy'}
        </Button>
        <Button
          variant="outline"
          size="lg"
          onClick={handleSaveAndBacktest}
        >
          <FlaskConical className="h-4 w-4" />
          Save & backtest
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
        initialExchangeId={state.exchangeId}
        initialSymbol={state.symbol}
        initialLeverage={state.leverage}
      />

      {/* Draft guard */}
      <ConfirmDialog
        open={discardOpen}
        destructive
        title="Discard unsaved changes?"
        primaryLabel="Discard changes"
        secondaryLabel="Keep editing"
        pendingLabel="Discarding…"
        pendingHint="Closing this won't cancel the discard."
        onConfirm={confirmDiscard}
        onClose={() => {
          setDiscardOpen(false);
          setPendingNav(null);
        }}
      >
        <p>The unsaved changes in the builder will be lost. This can&apos;t be undone.</p>
      </ConfirmDialog>
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
