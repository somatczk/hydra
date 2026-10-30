export const PERCENT_FIELDS = [
  'max_position_pct',
  'max_risk_per_trade',
  'max_portfolio_heat',
  'max_daily_loss_pct',
  'max_drawdown_pct',
] as const;

export type PercentField = (typeof PERCENT_FIELDS)[number];
export const COUNT_FIELDS = ['max_concurrent_positions', 'max_consecutive_losses'] as const;

export type CountField = (typeof COUNT_FIELDS)[number];
export type RiskLimitField = PercentField | CountField;
export type RiskLimitsForm = Record<RiskLimitField, string>;
export type RiskLimitValues = Record<RiskLimitField, number>;
export type RiskLimitsUpdate = Partial<RiskLimitValues>;

export const FIELDS: readonly RiskLimitField[] = [...PERCENT_FIELDS, ...COUNT_FIELDS];

export const LIMIT_LABELS: Record<RiskLimitField, string> = {
  max_position_pct: 'Max position size',
  max_risk_per_trade: 'Max risk per trade',
  max_portfolio_heat: 'Max portfolio heat',
  max_daily_loss_pct: 'Max daily loss',
  max_drawdown_pct: 'Max drawdown',
  max_concurrent_positions: 'Max concurrent positions',
  max_consecutive_losses: 'Max consecutive losses',
};

export const isCount = (f: RiskLimitField): f is CountField => (COUNT_FIELDS as readonly string[]).includes(f);

/** A stored limit exactly as the API holds it: "0.5%" for a fraction, "3" for a count. */
export function limitText(field: RiskLimitField, value: number): string {
  return isCount(field) ? String(value) : `${fractionToPercentInput(value)}%`;
}

/** Fraction (0.005) to the percent text shown in the form ("0.5"), without integer rounding. */
export function fractionToPercentInput(fraction: number): string {
  return String(Number((fraction * 100).toFixed(4)));
}

export function riskLimitsToForm(cfg: RiskLimitValues): RiskLimitsForm {
  const form = {} as RiskLimitsForm;
  for (const f of FIELDS) form[f] = isCount(f) ? String(cfg[f]) : fractionToPercentInput(cfg[f]);
  return form;
}

export type RiskLimitsUpdateResult =
  | { ok: true; update: RiskLimitsUpdate }
  | { ok: false; field: RiskLimitField };

/** Only the fields whose text changed since load, converted back to the API's units. */
export function buildRiskLimitsUpdate(
  baseline: RiskLimitsForm,
  current: RiskLimitsForm,
): RiskLimitsUpdateResult {
  const update: RiskLimitsUpdate = {};
  for (const f of FIELDS) {
    if (current[f] === baseline[f]) continue;
    const raw = current[f].trim();
    const n = raw === '' ? NaN : Number(raw);
    if (isCount(f)) {
      if (!Number.isInteger(n)) return { ok: false, field: f };
      update[f] = n;
    } else {
      if (!Number.isFinite(n)) return { ok: false, field: f };
      update[f] = Number((n / 100).toFixed(6));
    }
  }
  return { ok: true, update };
}

export interface LimitChange {
  field: RiskLimitField;
  from: number;
  to: number;
  /** Every limit is a ceiling, so a higher one lets more risk through. */
  looser: boolean;
}

export function limitChanges(current: RiskLimitValues, update: RiskLimitsUpdate): LimitChange[] {
  return FIELDS.flatMap((field) => {
    const to = update[field];
    return to === undefined || to === current[field] ? [] : [{ field, from: current[field], to, looser: to > current[field] }];
  });
}

// ---------------------------------------------------------------------------
// Per-strategy overrides (tighter-only): position size, risk per trade, concurrency
// ---------------------------------------------------------------------------

export const STRATEGY_FIELDS = [
  'max_position_pct',
  'max_risk_per_trade',
  'max_concurrent_positions',
] as const;

export type StrategyField = (typeof STRATEGY_FIELDS)[number];
export type StrategyOverrideValues = Record<StrategyField, number | null>;
export type StrategyOverrideForm = Record<StrategyField, string>;

export interface StrategyOverrideRow extends StrategyOverrideValues {
  strategy_id: string;
}

export interface RiskConfigResponse extends RiskLimitValues {
  overrides: StrategyOverrideRow[];
}

/** The enforced limits for one strategy: min(global, override) per cap, null -> global. */
export function effectiveOverride(
  cfg: RiskLimitValues,
  over: StrategyOverrideValues,
): StrategyOverrideValues {
  return {
    max_position_pct: over.max_position_pct === null ? cfg.max_position_pct : Math.min(over.max_position_pct, cfg.max_position_pct),
    max_risk_per_trade: over.max_risk_per_trade === null ? cfg.max_risk_per_trade : Math.min(over.max_risk_per_trade, cfg.max_risk_per_trade),
    max_concurrent_positions: over.max_concurrent_positions === null ? cfg.max_concurrent_positions : Math.min(over.max_concurrent_positions, cfg.max_concurrent_positions),
  };
}

/** The stored override as the form holds it: "" for no override, "0.5" for a percent. */
export function strategyOverrideToForm(over: StrategyOverrideValues): StrategyOverrideForm {
  const form = {} as StrategyOverrideForm;
  for (const f of STRATEGY_FIELDS) {
    const v = over[f];
    form[f] = v === null ? '' : isCount(f) ? String(v) : fractionToPercentInput(v);
  }
  return form;
}

export type StrategyOverrideBuild =
  | { ok: true; update: StrategyOverrideValues; clamped: StrategyField[] }
  | { ok: false; field: StrategyField };

/** All three caps as the PUT sends them (null clears); looser entries clamp to the global. */
export function buildStrategyOverrideUpdate(
  current: StrategyOverrideForm,
  cfg: RiskLimitValues,
): StrategyOverrideBuild {
  const update = {} as StrategyOverrideValues;
  const clamped: StrategyField[] = [];
  for (const f of STRATEGY_FIELDS) {
    const raw = current[f].trim();
    if (raw === '') {
      update[f] = null;
      continue;
    }
    const n = Number(raw);
    if (!Number.isFinite(n) || (isCount(f) && !Number.isInteger(n))) return { ok: false, field: f };
    const value = isCount(f) ? n : Number((n / 100).toFixed(6));
    const global = cfg[f];
    if (value > global) {
      // tighter-only: only the min of the two is ever enforced, so a looser entry
      // saves as the global value it would collapse to anyway
      update[f] = global;
      clamped.push(f);
    } else {
      update[f] = value;
    }
  }
  return { ok: true, update, clamped };
}

export function hasOverride(over: StrategyOverrideValues): boolean {
  return STRATEGY_FIELDS.some((f) => over[f] !== null);
}

/** "5%" / "3" for a stored override, "—" when that cap is not overridden. */
export function strategyLimitText(field: StrategyField, value: number | null): string {
  return value === null ? '—' : isCount(field) ? String(value) : `${fractionToPercentInput(value)}%`;
}
