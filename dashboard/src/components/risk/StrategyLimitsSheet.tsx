'use client';

import { useState } from 'react';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { TypedConfirm } from '@/components/ui/TypedConfirm';
import { plural } from '@/lib/derive';
import {
  LIMIT_LABELS,
  STRATEGY_FIELDS,
  buildStrategyOverrideUpdate,
  effectiveOverride,
  isCount,
  riskLimitsToForm,
  strategyLimitText,
  strategyOverrideToForm,
  type RiskLimitValues,
  type StrategyField,
  type StrategyOverrideForm,
  type StrategyOverrideValues,
} from '@/lib/riskLimits';

interface StrategyLimitsSheetProps {
  strategyId: string;
  /** The global limits the override can only tighten. */
  cfg: RiskLimitValues;
  stored: StrategyOverrideValues;
  /** True whenever a row exists in risk_config, all-null or not: any stored row is removable. */
  hasStoredRow: boolean;
  /** Resolves true once the engine has the new override; the editor then closes. */
  onSave: (update: StrategyOverrideValues) => Promise<boolean>;
  /** Resolves true once the engine dropped the override (DELETE); receives the typed reason. */
  onClear: (reason: string) => Promise<boolean>;
}

/** One strategy's tighter-only override: stored + effective limits, an editor that
 * clamps looser entries to the global value (with the reason shown), and a typed
 * removal. */
export function StrategyLimitsSheet({ strategyId, cfg, stored, hasStoredRow, onSave, onClear }: StrategyLimitsSheetProps) {
  const [form, setForm] = useState<StrategyOverrideForm | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [clamping, setClamping] = useState<{ update: StrategyOverrideValues; fields: StrategyField[] } | null>(null);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);
  const effective = effectiveOverride(cfg, stored);

  const save = async (update: StrategyOverrideValues) => {
    if (await onSave(update)) setForm(null);
  };

  const submit = async () => {
    if (!form) return;
    const built = buildStrategyOverrideUpdate(form, cfg);
    if (!built.ok) return setError(`${LIMIT_LABELS[built.field]} needs a valid number.`);
    const base = strategyOverrideToForm(stored);
    if (STRATEGY_FIELDS.every((f) => form[f].trim() === base[f].trim())) {
      return setError('No changes to save.');
    }
    setError(null);
    if (built.clamped.length > 0) return setClamping({ update: built.update, fields: built.clamped });
    await save(built.update);
  };

  if (!form) {
    return (
      <>
        <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-sm">
          {STRATEGY_FIELDS.map((f) => (
            <div key={f} className="contents">
              <dt className="text-ink-2">
                {LIMIT_LABELS[f]}
                <span className="block text-xs">override {strategyLimitText(f, stored[f])}</span>
              </dt>
              <dd className="text-right font-mono text-ink-1">{strategyLimitText(f, effective[f])}</dd>
            </div>
          ))}
        </dl>
        <div className="mt-4 flex gap-2">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setForm(strategyOverrideToForm(stored))}
          >
            Edit override
          </Button>
          {hasStoredRow && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setClearing(true);
                setClearError(null);
              }}
            >
              Remove override
            </Button>
          )}
        </div>
        <TypedConfirm
          open={clearing}
          title={`Remove the override for ${strategyId}?`}
          token={strategyId}
          primaryLabel="Remove override"
          secondaryLabel="Keep it"
          pendingLabel="Removing…"
          error={clearError}
          onClose={() => setClearing(false)}
          onConfirm={async (reason) => {
            if (await onClear(reason)) {
              setClearing(false);
              setClearError(null);
            } else {
              setClearError('Not removed.');
            }
          }}
        >
          <p>The global limits apply to {strategyId} again once the engine reloads.</p>
        </TypedConfirm>
      </>
    );
  }

  return (
    <>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {STRATEGY_FIELDS.map((f) => (
          <Input
            key={f}
            label={`${LIMIT_LABELS[f]}${isCount(f) ? '' : ' (%)'}`}
            type="number"
            step="any"
            inputMode="decimal"
            placeholder={riskLimitsToForm(cfg)[f]}
            value={form[f]}
            onChange={(e) => setForm({ ...form, [f]: e.target.value })}
          />
        ))}
      </div>
      <p className="mt-3 text-xs text-ink-2">
        Leave a field empty for no override; only the tighter of the strategy and global limits is ever enforced.
      </p>
      {error && (
        <p role="alert" className="mt-3 text-sm text-danger">
          {error}
        </p>
      )}
      <div className="mt-4 flex gap-2">
        <Button onClick={() => void submit()}>Save override</Button>
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
        open={clamping !== null}
        title={`Clamp ${plural(clamping?.fields.length ?? 0, 'looser value')} to the global limits?`}
        primaryLabel="Save clamped override"
        secondaryLabel="Keep editing"
        pendingLabel="Saving…"
        pendingHint="Closing this won't cancel the save."
        onClose={() => setClamping(null)}
        onConfirm={async () => {
          if (!clamping) return;
          await save(clamping.update);
          setClamping(null);
        }}
      >
        {clamping?.fields.map((f) => (
          <p key={f}>
            {LIMIT_LABELS[f]} {form[f]} → <b>{strategyLimitText(f, clamping.update[f])}</b> — that is the
            global limit, and only the tighter of the strategy and global limits is ever enforced.
          </p>
        ))}
      </ConfirmDialog>
    </>
  );
}
