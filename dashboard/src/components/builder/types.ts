/** Type definitions for the visual strategy builder. */

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
}

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
  method: 'atr' | 'fixed_pct';
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

/* ---------- Full builder state ---------- */

export interface BuilderState {
  entryLong: BuilderConditionGroup;
  exitLong: BuilderConditionGroup;
  entryShort: BuilderConditionGroup;
  exitShort: BuilderConditionGroup;
  timeframes: TimeframeConfig;
  risk: RiskConfig;
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
}

export interface SerializedCondition {
  indicator: string;
  params: ConditionParams;
  comparator: string;
  value: number | string;
}

export interface SerializedConditionGroup {
  operator: string;
  conditions: SerializedCondition[];
}

/* ---------- Reducer actions ---------- */

export type BuilderAction =
  | { type: 'ADD_CONDITION'; section: RuleSection }
  | { type: 'REMOVE_CONDITION'; section: RuleSection; conditionId: string }
  | {
      type: 'UPDATE_CONDITION';
      section: RuleSection;
      conditionId: string;
      field: keyof BuilderCondition;
      value: string | number | ConditionParams;
    }
  | { type: 'SET_OPERATOR'; section: RuleSection; operator: 'AND' | 'OR' }
  | { type: 'SET_TIMEFRAME'; field: keyof TimeframeConfig; value: string }
  | { type: 'SET_RISK'; risk: Partial<RiskConfig> }
  | { type: 'SET_STOP_LOSS'; stopLoss: Partial<StopLossConfig> }
  | { type: 'SET_TAKE_PROFIT'; takeProfit: Partial<TakeProfitConfig> }
  | { type: 'SET_SIZING'; sizing: Partial<SizingConfig> };

export type RuleSection = 'entryLong' | 'exitLong' | 'entryShort' | 'exitShort';
