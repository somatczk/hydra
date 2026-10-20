export const PERCENT_FIELDS = [
  'max_position_pct',
  'max_risk_per_trade',
  'max_portfolio_heat',
  'max_daily_loss_pct',
  'max_drawdown_pct',
] as const;

export type PercentField = (typeof PERCENT_FIELDS)[number];
export type RiskLimitField = PercentField | 'max_concurrent_positions';
export type RiskLimitsForm = Record<RiskLimitField, string>;
export type RiskLimitValues = Record<RiskLimitField, number>;
export type RiskLimitsUpdate = Partial<RiskLimitValues>;

const FIELDS: readonly RiskLimitField[] = [...PERCENT_FIELDS, 'max_concurrent_positions'];

/** Fraction (0.005) to the percent text shown in the form ("0.5"), without integer rounding. */
export function fractionToPercentInput(fraction: number): string {
  return String(Number((fraction * 100).toFixed(4)));
}

export function riskLimitsToForm(cfg: RiskLimitValues): RiskLimitsForm {
  const form = { max_concurrent_positions: String(cfg.max_concurrent_positions) } as RiskLimitsForm;
  for (const f of PERCENT_FIELDS) form[f] = fractionToPercentInput(cfg[f]);
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
    if (f === 'max_concurrent_positions') {
      if (!Number.isInteger(n)) return { ok: false, field: f };
      update[f] = n;
    } else {
      if (!Number.isFinite(n)) return { ok: false, field: f };
      update[f] = Number((n / 100).toFixed(6));
    }
  }
  return { ok: true, update };
}
