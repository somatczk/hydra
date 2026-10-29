/** A condition's bindings to top-level strategy `parameters` (strategy/condition_schema.py Condition): what hyperopt
 * and walk-forward rewire. The builder never authors them; it carries them through untouched and drops only the ones a
 * user edit invalidates. `param_overrides: {}` is an explicit opt-out of the implicit `{indicator}_{param}` binding. */

import type { ConditionParams } from './types';

export interface ConditionBindings {
  param_key?: string;
  param_overrides?: Record<string, string>;
  value_param_key?: string;
  value_ref_overrides?: Record<string, string>;
}

const KEYS = ['param_key', 'param_overrides', 'value_param_key', 'value_ref_overrides'] as const;

/** The binding fields a stored condition carries; the API sends absent ones as null. */
export function bindingsOf(condition: Partial<Record<(typeof KEYS)[number], unknown>>): ConditionBindings | undefined {
  const out: Record<string, unknown> = {};
  for (const k of KEYS) if (condition[k] !== null && condition[k] !== undefined) out[k] = condition[k];
  return Object.keys(out).length > 0 ? (out as ConditionBindings) : undefined;
}

/** Drop one side's bindings: 'indicator' when the condition's indicator changes, 'value' when what it compares to
 * changes kind or reference. Returns what stays and the top-level names that no longer bind. */
export function dropBindings(
  bindings: ConditionBindings | undefined,
  side: 'indicator' | 'value',
): { kept: ConditionBindings | undefined; dropped: string[] } {
  if (!bindings) return { kept: undefined, dropped: [] };
  const kept = { ...bindings };
  let dropped: string[];
  if (side === 'indicator') {
    dropped = [...(kept.param_key ? [kept.param_key] : []), ...Object.keys(kept.param_overrides ?? {})];
    delete kept.param_key;
    if (Object.keys(kept.param_overrides ?? {}).length > 0) delete kept.param_overrides;
  } else {
    dropped = [...(kept.value_param_key ? [kept.value_param_key] : []), ...Object.keys(kept.value_ref_overrides ?? {})];
    delete kept.value_param_key;
    delete kept.value_ref_overrides;
  }
  return { kept: bindingsOf(kept), dropped };
}

/** One line naming every binding, so a hand-written strategy's wiring is visible in the builder. */
export function bindingsText(bindings: ConditionBindings | undefined, params: ConditionParams): string | null {
  if (!bindings) return null;
  const parts: string[] = [];
  if (bindings.param_key) parts.push(`${bindings.param_key} → ${Object.keys(params)[0] ?? 'the first parameter'}`);
  for (const [name, param] of Object.entries(bindings.param_overrides ?? {})) parts.push(`${name} → ${param}`);
  if (bindings.param_overrides && Object.keys(bindings.param_overrides).length === 0) parts.push('no implicit binding');
  if (bindings.value_param_key) parts.push(`${bindings.value_param_key} → value`);
  for (const [name, param] of Object.entries(bindings.value_ref_overrides ?? {})) parts.push(`${name} → compared ${param}`);
  return parts.length > 0 ? `Strategy parameter bindings: ${parts.join(', ')}` : null;
}
