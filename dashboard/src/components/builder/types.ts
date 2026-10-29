/** Type definitions for the visual strategy builder. */

import type { Schedule, WireSchedule } from '@/lib/schedule';
import type { ConditionBindings } from './bindings';

/* ---------- Indicator API types ---------- */

export interface ParamSchema {
  name: string;
  type: 'int' | 'float';
  default: number | null;
  min: number | null;
  max: number | null;
}

export interface IndicatorSchema {
  name: string;
  category: 'trend' | 'momentum' | 'volatility' | 'volume' | 'other';
  description: string;
  params: ParamSchema[];
}

export interface ComparatorSchema {
  value: string;
  label: string;
  description: string;
}

/* ---------- Condition tree types (mirrors Python Pydantic models) ---------- */

export interface ConditionParams {
  [key: string]: number;
}

export interface BuilderCondition {
  id: string;
  indicator: string;
  params: ConditionParams;
  comparator: string;
  value: number | string;
  /** Carried through from the stored condition; the builder never authors these. */
  bindings?: ConditionBindings;
}

/** A condition edit applied in one reducer step; `edit` marks a user change to a bound value (it moves the tunable). */
export type ConditionPatch = Partial<Omit<BuilderCondition, 'id'>>;

export interface BuilderConditionGroup {
  operator: 'AND' | 'OR';
  conditions: BuilderCondition[];
}

/* ---------- Timeframe / Risk types ---------- */

export interface TimeframeConfig {
  primary: string;
  confirmation?: string;
  entry?: string;
}

export interface StopLossConfig {
  method: 'atr' | 'fixed_pct' | 'trailing_stop';
  value: number;
}

export interface TakeProfitConfig {
  method: 'atr' | 'fixed_pct';
  value: number;
}

export interface SizingConfig {
  method: 'fixed_fractional' | 'atr_based' | 'kelly' | 'volatility_scaled';
  riskPerTradePct: number;
  maxPositionPct: number;
}

export interface RiskConfig {
  stopLoss: StopLossConfig;
  takeProfit: TakeProfitConfig;
  sizing: SizingConfig;
}

/* ---------- ML Overlay ---------- */

export interface MlOverlay {
  modelName: string;
  confidenceThreshold: number;
}

/* ---------- Full builder state ---------- */

export interface BuilderState {
  entryLong: BuilderConditionGroup;
  exitLong: BuilderConditionGroup;
  entryShort: BuilderConditionGroup;
  exitShort: BuilderConditionGroup;
  timeframes: TimeframeConfig;
  risk: RiskConfig;
  mlOverlay: MlOverlay | null;
  editingId: string | null;
  strategyName: string;
  strategyDescription: string;
  exchangeId: string;
  symbol: string;
  /** P4-F: the perp leverage (1-3×) — applies to binanceusdm FUTURES strategies only; spot stays 1×. */
  leverage: number;
  /** The entry schedule (strategy/config.py ScheduleConfig); null is always active. */
  schedule: Schedule | null;
  /** Stored top-level values of names the rules bind; kept equal to every site they set. */
  tunables: Record<string, number>;
  baseline: string;
}

/* ---------- Preview types ---------- */

export interface PreviewSignal {
  timestamp: string;
  type: string;
  price: number;
}

export interface PreviewMetrics {
  trades: number;
  win_rate: number;
  pnl: number;
}

export interface PreviewResponse {
  signals: PreviewSignal[];
  metrics: PreviewMetrics;
}

/* ---------- Save types ---------- */

export interface SaveRequest {
  name: string;
  description: string;
  exchange_id: string;
  symbol: string;
  rules: {
    entry_long: SerializedConditionGroup | null;
    exit_long: SerializedConditionGroup | null;
    entry_short: SerializedConditionGroup | null;
    exit_short: SerializedConditionGroup | null;
  };
  timeframes: { primary: string; confirmation?: string; entry?: string };
  risk: {
    stop_loss_method: string;
    stop_loss_value: number;
    take_profit_method: string;
    take_profit_value: number;
    sizing_method: string;
    sizing_params: Record<string, number>;
  };
  enable_immediately: boolean;
  ml_overlay?: { model_name: string; confidence_threshold: number } | null;
  /** P4: perp strategies only (1-3×, isolated); the server refuses it on spot venues. */
  leverage?: number;
  /** On update `{}` clears the stored schedule; on save it writes none. */
  schedule?: WireSchedule;
  /** Top-level tunables by name; only names the rules bind (`{}` clears on update). */
  parameters?: Record<string, number>;
}

export interface SerializedCondition {
  indicator: string;
  params: ConditionParams;
  comparator: string;
  value: number | string;
  /** Hyperopt bindings (strategy/condition_schema.py); the detail sends absent ones as null. */
  param_key?: string | null;
  param_overrides?: Record<string, string> | null;
  value_param_key?: string | null;
  value_ref_overrides?: Record<string, string> | null;
}

export interface SerializedConditionGroup {
  operator: string;
  conditions: SerializedCondition[];
}

/* ---------- Strategy CRUD types ---------- */

export interface StrategySummary {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  filename: string;
}

export interface StrategyDetail {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  exchange_id: string;
  symbol: string;
  rules: {
    entry_long: SerializedConditionGroup | null;
    exit_long: SerializedConditionGroup | null;
    entry_short: SerializedConditionGroup | null;
    exit_short: SerializedConditionGroup | null;
  };
  timeframes: { primary: string; confirmation?: string; entry?: string };
  risk: {
    stop_loss_method: string;
    stop_loss_value: number;
    take_profit_method: string;
    take_profit_value: number;
    sizing_method: string;
    sizing_params: Record<string, number>;
  };
  ml_overlay?: { model_name: string; confidence_threshold: number } | null;
  /** P4-F: the stored perp leverage for a binanceusdm strategy, so an edit round-trips it. */
  leverage?: number;
  schedule?: WireSchedule | null;
  /** Stored top-level `parameters` other than rules / required_history / description. */
  parameters?: Record<string, unknown>;
}

/* ---------- Reducer actions ---------- */

export type BuilderAction =
  | { type: 'ADD_CONDITION'; section: RuleSection }
  | { type: 'REMOVE_CONDITION'; section: RuleSection; conditionId: string }
  | { type: 'UPDATE_CONDITION'; section: RuleSection; conditionId: string; patch: ConditionPatch; edit?: boolean }
  | { type: 'SET_TUNABLE'; name: string; value: number | null }
  | { type: 'SET_OPERATOR'; section: RuleSection; operator: 'AND' | 'OR' }
  | { type: 'SET_TIMEFRAME'; field: keyof TimeframeConfig; value: string }
  | { type: 'SET_RISK'; risk: Partial<RiskConfig> }
  | { type: 'SET_STOP_LOSS'; stopLoss: Partial<StopLossConfig> }
  | { type: 'SET_TAKE_PROFIT'; takeProfit: Partial<TakeProfitConfig> }
  | { type: 'SET_SIZING'; sizing: Partial<SizingConfig> }
  | { type: 'SET_ML_OVERLAY'; mlOverlay: MlOverlay | null }
  | { type: 'SET_SCHEDULE'; schedule: Schedule | null }
  | { type: 'LOAD_STRATEGY'; payload: StrategyDetail }
  | { type: 'MARK_CLEAN' }
  | { type: 'RESET' };

export type RuleSection = 'entryLong' | 'exitLong' | 'entryShort' | 'exitShort';
