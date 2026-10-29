'use client';

import { Button } from '@/components/ui/Button';
import { DataCard } from '@/components/ui/DataCard';
import { Input } from '@/components/ui/Input';
import { bindableParams, parseIndicatorRef, RULE_GROUPS, type BindingSite } from './bindings';
import type { BuilderState } from './types';

const GROUP_TITLE: Record<string, string> = {
  entry_long: 'Entry long',
  exit_long: 'Exit long',
  entry_short: 'Entry short',
  exit_short: 'Exit short',
};
const SECTION_OF = Object.fromEntries(RULE_GROUPS);

function siteLabel(state: BuilderState, site: BindingSite): string {
  const condition = state[SECTION_OF[site.group]].conditions[site.index];
  const what =
    site.target === 'params'
      ? `${condition?.indicator} ${site.key}`
      : site.target === 'value'
        ? 'value'
        : `compared ${parseIndicatorRef(String(condition?.value)).name} ${site.key}`;
  return `${GROUP_TITLE[site.group]} #${site.index + 1} · ${what}`;
}

interface TunedParametersProps {
  state: BuilderState;
  /** A number stores the value and writes it into every site; null forgets it (the conditions keep their values). */
  onSet: (name: string, value: number | null) => void;
}

/** The top-level `parameters` the rules bind: what hyperopt and walk-forward tune, editable in one place. */
export function TunedParameters({ state, onSet }: TunedParametersProps) {
  const bound = Object.entries(bindableParams(state));
  return (
    <DataCard
      title="Tuned parameters"
      description="Strategy parameters the rules bind, by name: what hyperopt and walk-forward tune. A stored value is written into every condition it binds, and editing a bound condition updates it here."
    >
      {bound.length === 0 ? (
        <p className="text-sm text-ink-2">No condition binds a strategy parameter.</p>
      ) : (
        <ul className="flex flex-col gap-3" data-testid="tuned-parameters">
          {bound.map(([name, sites]) => {
            const stored = state.tunables[name];
            const shown = stored ?? sites[0].value;
            return (
              <li key={name} className="flex items-end gap-2">
                <div className="min-w-0 flex-1">
                  <Input
                    label={name}
                    type="number"
                    value={shown ?? ''}
                    onChange={(e) => {
                      const v = parseFloat(e.target.value);
                      if (!isNaN(v)) onSet(name, v);
                    }}
                    hint={`${sites.map((s) => siteLabel(state, s)).join('; ')}${stored === undefined ? ' · from the condition, not stored' : ''}`}
                    className="font-mono"
                  />
                </div>
                {stored !== undefined && (
                  <Button variant="ghost" size="sm" className="mb-6" onClick={() => onSet(name, null)} aria-label={`Clear ${name}`}>
                    Clear
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </DataCard>
  );
}
