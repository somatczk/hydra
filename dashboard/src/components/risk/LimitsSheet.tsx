'use client';

import { useState } from 'react';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { plural } from '@/lib/derive';
import {
  FIELDS,
  LIMIT_LABELS,
  buildRiskLimitsUpdate,
  isCount,
  limitChanges,
  limitText,
  riskLimitsToForm,
  type LimitChange,
  type RiskLimitValues,
  type RiskLimitsForm,
  type RiskLimitsUpdate,
} from '@/lib/riskLimits';

interface LimitsSheetProps {
  cfg: RiskLimitValues;
  /** Resolves true once the engine has the new limits; the editor then closes. */
  onSave: (update: RiskLimitsUpdate) => Promise<boolean>;
}

/** The stored limits, an editor, and an L2 diff before anything looser is saved (C-LIM). */
export function LimitsSheet({ cfg, onSave }: LimitsSheetProps) {
  const [form, setForm] = useState<{ baseline: RiskLimitsForm; current: RiskLimitsForm } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loosening, setLoosening] = useState<{ update: RiskLimitsUpdate; changes: LimitChange[] } | null>(null);

  const save = async (update: RiskLimitsUpdate) => {
    if (await onSave(update)) setForm(null);
  };

  const submit = async () => {
    if (!form) return;
    const built = buildRiskLimitsUpdate(form.baseline, form.current);
    if (!built.ok) return setError(`${LIMIT_LABELS[built.field]} needs a valid number.`);
    const changes = limitChanges(cfg, built.update);
    if (changes.length === 0) return setError('No changes to save.');
    setError(null);
    if (changes.some((c) => c.looser)) return setLoosening({ update: built.update, changes });
    await save(built.update);
  };

  if (!form) {
    return (
      <>
        <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-sm">
          {FIELDS.map((f) => (
            <div key={f} className="contents">
              <dt className="text-ink-2">{LIMIT_LABELS[f]}</dt>
              <dd className="text-right font-mono text-ink-1">{limitText(f, cfg[f])}</dd>
            </div>
          ))}
        </dl>
        <Button
          className="mt-4"
          variant="secondary"
          size="sm"
          onClick={() => {
            const base = riskLimitsToForm(cfg);
            setForm({ baseline: base, current: base });
          }}
        >
          Edit limits
        </Button>
      </>
    );
  }

  const looser = loosening?.changes.filter((c) => c.looser).length ?? 0;
  return (
    <>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {FIELDS.map((f) => (
          <Input
            key={f}
            label={`${LIMIT_LABELS[f]}${isCount(f) ? '' : ' (%)'}`}
            type="number"
            step="any"
            inputMode="decimal"
            value={form.current[f]}
            onChange={(e) => setForm({ ...form, current: { ...form.current, [f]: e.target.value } })}
          />
        ))}
      </div>
      {error && (
        <p role="alert" className="mt-3 text-sm text-danger">
          {error}
        </p>
      )}
      <div className="mt-4 flex gap-2">
        <Button onClick={() => void submit()}>Save limits</Button>
        <Button
          variant="ghost"
          onClick={() => {
            setForm(null);
            setError(null);
          }}
        >
          Cancel
        </Button>
      </div>
      <ConfirmDialog
        open={loosening !== null}
        title={`Loosen ${plural(looser, 'risk limit')}?`}
        primaryLabel="Save looser limits"
        secondaryLabel="Keep current"
        pendingLabel="Saving…"
        pendingHint="Closing this won't cancel the save."
        onClose={() => setLoosening(null)}
        onConfirm={async () => {
          if (!loosening) return;
          await save(loosening.update);
          setLoosening(null);
        }}
      >
        {loosening?.changes.map((c) => (
          <p key={c.field}>
            {LIMIT_LABELS[c.field]} {limitText(c.field, c.from)} → <b>{limitText(c.field, c.to)}</b>
            {c.looser && ' (looser)'}
          </p>
        ))}
      </ConfirmDialog>
    </>
  );
}
