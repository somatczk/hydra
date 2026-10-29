/** A condition's bindings to top-level strategy `parameters` (strategy/condition_schema.py Condition): what hyperopt
 * and walk-forward rewire. The builder never authors them; it carries them through untouched and drops only the ones a
 * user edit invalidates. `param_overrides: {}` is an explicit opt-out of the implicit `{indicator}_{param}` binding. */

import type { BuilderState, ConditionParams, IndicatorSchema, RuleSection } from './types';

export interface ConditionBindings {
  param_key?: string;
  param_overrides?: Record<string, string>;
  value_param_key?: string;
  value_ref_overrides?: Record<string, string>;
}

const KEYS = ['param_key', 'param_overrides', 'value_param_key', 'value_ref_overrides'] as const;

/** The binding fields a stored condition carries; the API sends absent ones as null. */
export function bindingsOf(condition: object): ConditionBindings | undefined {
  const fields = condition as Partial<Record<(typeof KEYS)[number], unknown>>;
  const out: Record<string, unknown> = {};
  for (const k of KEYS) if (fields[k] !== null && fields[k] !== undefined) out[k] = fields[k];
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

/** Each indicator's params with their registry defaults (`GET /api/strategies/indicators`): the signature the
 * engine's indicator_params reads. A param with no default is null. */
export type Catalogue = Record<string, Record<string, number | null>>;

export function catalogueOf(indicators: IndicatorSchema[]): Catalogue {
  return Object.fromEntries(indicators.map((i) => [i.name, Object.fromEntries(i.params.map((p) => [p.name, p.default]))]));
}

/** The override names whose target the indicator does not take (`known`, its registry params). The engine
 * refuses such a `param_overrides` target at save and skips such a `value_ref_overrides` key. An indicator the
 * catalogue does not list (`known` undefined) is left to the engine. */
export function unknownTargets(overrides: Record<string, string> | undefined, known: string[] | undefined): string[] {
  if (!known) return [];
  return Object.entries(overrides ?? {})
    .filter(([, key]) => !known.includes(key))
    .map(([name]) => name);
}

/** A condition value naming another indicator, `"name:k=v,k=v"`, as rule_engine.parse_indicator_ref reads it. */
export function parseIndicatorRef(ref: string): { name: string; params: ConditionParams } {
  const [name, args = ''] = ref.split(':');
  const params: ConditionParams = {};
  for (const kv of args.split(',')) {
    const [k, v] = kv.split('=');
    if (k?.trim() && v !== undefined && v.trim() !== '' && Number.isFinite(Number(v))) params[k.trim()] = Number(v);
  }
  return { name, params };
}

export function formatIndicatorRef(name: string, params: ConditionParams): string {
  const args = Object.entries(params).map(([k, v]) => `${k}=${v}`).join(',');
  return args ? `${name}:${args}` : name;
}

/* ---------- The resolver (strategy/builtin/rule_based.py `param_bindings`), ported for live edits ---------- */

/** The rule groups in the resolver's order, with the builder's key for each. */
export const RULE_GROUPS: ReadonlyArray<readonly [string, RuleSection]> = [
  ['entry_long', 'entryLong'],
  ['exit_long', 'exitLong'],
  ['entry_short', 'entryShort'],
  ['exit_short', 'exitShort'],
];

/** One condition site a top-level parameter sets (rule_based.ParamBinding). */
export interface BindingSite {
  group: string;
  index: number;
  target: 'params' | 'value' | 'ref';
  key: string;
  value: number | undefined;
  implicit: boolean;
}

interface BindableCondition {
  indicator: string;
  params: ConditionParams;
  value: number | string;
  bindings?: ConditionBindings;
}

type BindableRules = Pick<BuilderState, RuleSection> | Record<RuleSection, { conditions: BindableCondition[] }>;

/** Every top-level name the rules bind, with its sites in rule order. With the `catalogue` it binds as the engine
 * does: an override the condition's params lack takes the indicator's registry default, and a `value_ref_overrides`
 * key the compared indicator does not take is skipped. A `param_overrides` target the indicator does not take still
 * binds (the engine refuses it at save; unknownTargets flags it), and an indicator the catalogue does not list binds
 * from its params alone. */
export function paramBindings(rules: BindableRules, catalogue?: Catalogue): Record<string, BindingSite[]> {
  const table: Record<string, BindingSite[]> = {};
  const valueOf = (indicator: string, params: ConditionParams, key: string) =>
    params[key] ?? catalogue?.[indicator]?.[key] ?? undefined;
  const bind = (name: string, site: BindingSite) => {
    (table[name] ??= []).push(site);
  };
  for (const [group, section] of RULE_GROUPS) {
    (rules[section]?.conditions ?? []).forEach((cond: BindableCondition, index: number) => {
      const b = cond.bindings ?? {};
      const site = (target: BindingSite['target'], key: string, value: number | undefined, implicit = false) =>
        ({ group, index, target, key, value, implicit }) satisfies BindingSite;
      const firstKey = Object.keys(cond.params)[0];
      if (b.param_key && firstKey !== undefined) bind(b.param_key, site('params', firstKey, cond.params[firstKey]));
      for (const [name, key] of Object.entries(b.param_overrides ?? {})) {
        bind(name, site('params', key, valueOf(cond.indicator, cond.params, key)));
      }
      if (b.value_param_key && typeof cond.value === 'number') bind(b.value_param_key, site('value', 'value', cond.value));
      const ref = typeof cond.value === 'string' ? parseIndicatorRef(cond.value) : null;
      if (b.value_ref_overrides && ref) {
        const known = catalogue?.[ref.name];
        for (const [name, key] of Object.entries(b.value_ref_overrides)) {
          if (!known || key in known) bind(name, site('ref', key, valueOf(ref.name, ref.params, key)));
        }
      }
      if (!b.param_key && b.param_overrides === undefined) {
        for (const [key, value] of Object.entries(cond.params)) bind(`${cond.indicator}_${key}`, site('params', key, value, true));
      }
      if (b.value_ref_overrides === undefined && ref) {
        for (const [key, value] of Object.entries(ref.params)) bind(`${ref.name}_${key}`, site('ref', key, value, true));
      }
    });
  }
  return table;
}

/** An implicit name-match whose sites hold different values never binds (rule_based.ambiguous). */
export function ambiguous(sites: BindingSite[]): boolean {
  return sites.some((s) => s.implicit) && new Set(sites.map((s) => s.value)).size > 1;
}

/** The names a top-level parameter can set (rule_based.bindable_params). */
export function bindableParams(rules: BindableRules, catalogue?: Catalogue): Record<string, BindingSite[]> {
  return Object.fromEntries(Object.entries(paramBindings(rules, catalogue)).filter(([, sites]) => !ambiguous(sites)));
}
