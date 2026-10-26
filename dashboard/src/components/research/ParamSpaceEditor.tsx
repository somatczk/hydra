'use client';

import { useEffect, useRef, useState } from 'react';
import { Input } from '@/components/ui/Input';
import { paramSpaceBody, type StrategyParamDef, type WireParamDef } from '@/lib/research';

/** One editable range; text while typing, numbers only at the wire. */
interface Row {
  name: string;
  label: string;
  type: string;
  default: number;
  min: string;
  max: string;
}

function rowsFromDefs(defs: StrategyParamDef[]): Row[] {
  return defs.map((d) => ({
    name: d.name,
    label: d.label ?? d.name,
    type: d.type,
    default: d.default,
    min: String(d.min),
    max: String(d.max),
  }));
}

/** The wire param_space, or null when a range is empty, unparseable or not ascending. */
function wireSpace(rows: Row[]): WireParamDef[] | null {
  const parsed = rows.map((r) => ({
    name: r.name,
    type: r.type,
    default: r.default,
    min: Number(r.min),
    max: Number(r.max),
  }));
  if (parsed.some((r) => !Number.isFinite(r.min) || !Number.isFinite(r.max) || r.max <= r.min)) {
    return null;
  }
  return paramSpaceBody(parsed);
}

interface ParamSpaceEditorProps {
  /** GET /api/backtest/strategies/{id}/params; null while it loads. */
  defs: StrategyParamDef[] | null;
  /** The wire param_space: null while loading, when the strategy has none, or while a range is invalid. */
  onChange: (space: WireParamDef[] | null) => void;
  disabled?: boolean;
}

/** Min/Max overrides on top of a strategy's optimisable parameters; emits the wire param_space. */
export function ParamSpaceEditor({ defs, onChange, disabled }: ParamSpaceEditorProps) {
  const [rows, setRows] = useState<Row[]>(() => rowsFromDefs(defs ?? []));
  // read through a ref so a parent's re-render with a fresh inline handler cannot re-seed the rows
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  });

  useEffect(() => {
    const next = rowsFromDefs(defs ?? []);
    setRows(next);
    onChangeRef.current(defs !== null && next.length > 0 ? wireSpace(next) : null);
  }, [defs]);

  const update = (name: string, field: 'min' | 'max', value: string) => {
    const next = rows.map((r) => (r.name === name ? { ...r, [field]: value } : r));
    setRows(next);
    onChange(wireSpace(next));
  };

  if (defs === null) return <p className="text-sm text-ink-2">Reading parameters…</p>;
  if (defs.length === 0) {
    return <p className="text-sm text-ink-2">This strategy has no optimisable parameters.</p>;
  }

  return (
    <div className="flex flex-col gap-4" data-testid="param-space">
      {rows.map((row) => {
        const min = Number(row.min);
        const max = Number(row.max);
        const invalid = !Number.isFinite(min) || !Number.isFinite(max) || max <= min;
        return (
          <div key={row.name} className="grid grid-cols-1 items-end gap-3 sm:grid-cols-3">
            <div className="pb-2">
              <p className="text-sm font-medium text-ink-1">{row.label}</p>
              <p className="text-xs text-ink-2">{`default ${row.default}`}</p>
            </div>
            <Input
              label="Min"
              type="number"
              aria-label={`${row.label} min`}
              value={row.min}
              disabled={disabled}
              onChange={(e) => update(row.name, 'min', e.target.value)}
            />
            <Input
              label="Max"
              type="number"
              aria-label={`${row.label} max`}
              value={row.max}
              disabled={disabled}
              error={invalid ? 'Max must be greater than min.' : undefined}
              onChange={(e) => update(row.name, 'max', e.target.value)}
            />
          </div>
        );
      })}
    </div>
  );
}
