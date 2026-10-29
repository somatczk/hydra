/** The top-level tunables as the builder's single truth: a stored value is written into every site its name binds,
 * exactly as RuleBasedStrategy._apply_param_overrides does at runtime, so the builder shows what runs. */

import { bindableParams, formatIndicatorRef, parseIndicatorRef, RULE_GROUPS, type BindingSite } from './bindings';
import type { BuilderCondition, BuilderState, RuleSection } from './types';

const SECTION_OF: Record<string, RuleSection> = Object.fromEntries(RULE_GROUPS);

function setSite(state: BuilderState, site: BindingSite, value: number): BuilderState {
  const section = SECTION_OF[site.group];
  const conditions = state[section].conditions;
  const c = conditions[site.index];
  let next: BuilderCondition;
  if (site.target === 'params') {
    if (c.params[site.key] === value) return state;
    next = { ...c, params: { ...c.params, [site.key]: value } };
  } else if (site.target === 'value') {
    if (c.value === value) return state;
    next = { ...c, value };
  } else {
    const ref = parseIndicatorRef(String(c.value));
    if (ref.params[site.key] === value) return state;
    next = { ...c, value: formatIndicatorRef(ref.name, { ...ref.params, [site.key]: value }) };
  }
  return { ...state, [section]: { ...state[section], conditions: conditions.map((x, i) => (i === site.index ? next : x)) } };
}

/** Drop tunables no rule binds any more, take a user's edit of a bound value on `edited` into its tunable, then
 * write every tunable into all of its sites. */
export function reconcileTunables(
  state: BuilderState,
  edited?: { section: RuleSection; conditionId: string },
): BuilderState {
  const bound = bindableParams(state);
  const tunables: Record<string, number> = {};
  for (const [name, value] of Object.entries(state.tunables)) if (name in bound) tunables[name] = value;
  if (edited) {
    const group = RULE_GROUPS.find(([, section]) => section === edited.section)?.[0];
    const index = state[edited.section].conditions.findIndex((c) => c.id === edited.conditionId);
    for (const name of Object.keys(tunables)) {
      const site = bound[name].find((s) => s.group === group && s.index === index && typeof s.value === 'number');
      if (site?.value !== undefined && site.value !== tunables[name]) tunables[name] = site.value;
    }
  }
  let next: BuilderState = { ...state, tunables };
  for (const [name, value] of Object.entries(tunables)) {
    for (const site of bound[name]) next = setSite(next, site, value);
  }
  return next;
}

/** The detail's stored `parameters`, numeric values only (the engine accepts nothing else for a tunable). */
export function storedTunables(parameters: Record<string, unknown> | undefined): Record<string, number> {
  return Object.fromEntries(
    Object.entries(parameters ?? {}).filter((e): e is [string, number] => typeof e[1] === 'number' && Number.isFinite(e[1])),
  );
}
