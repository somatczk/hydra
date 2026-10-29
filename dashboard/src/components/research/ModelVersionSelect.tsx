'use client';

import { useEffect, useState } from 'react';
import { Select } from '@/components/ui/Select';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { fetchApi } from '@/lib/api';
import { stageBadge } from '@/lib/research';

/** The `GET /api/models` fields the pin needs. */
interface RegistryVersion {
  id: string;
  name: string;
  version: number;
  stage: string;
}

/**
 * The research pin (`model_version_id`): Production by default, or one registry version of a
 * model the strategy serves, any stage, for this run only. Renders nothing for a strategy
 * that serves no model.
 */
export function ModelVersionSelect({
  modelNames,
  value,
  onChange,
  disabled,
}: {
  modelNames: string[];
  value: string | null;
  onChange: (id: string | null) => void;
  disabled?: boolean;
}) {
  const [versions, setVersions] = useState<RegistryVersion[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  const serves = modelNames.length > 0;

  useEffect(() => {
    if (!serves) return;
    let cancelled = false;
    setError(null);
    fetchApi<RegistryVersion[]>('/api/models')
      .then((rows) => {
        if (!cancelled) setVersions(rows);
      })
      .catch((err) => {
        if (!cancelled) setError(err);
      });
    return () => {
      cancelled = true;
    };
  }, [serves, reload]);

  if (!serves) return null;
  if (error !== null) {
    return (
      <DataStateErrorCard
        error={error}
        fallback="Failed to load model versions"
        onRetry={() => setReload((n) => n + 1)}
      />
    );
  }

  const mine = (versions ?? []).filter((v) => modelNames.includes(v.name));
  const isProduction = (v: RegistryVersion) => stageBadge(v.stage).label === 'Production';
  const production = modelNames.map((name) => mine.find((v) => v.name === name && isProduction(v)));
  const current = modelNames.map((name, i) => (production[i] ? `${name} v${production[i].version}` : `${name}: none`));
  const options = [
    { value: '', label: versions === null ? 'Production' : `Production (${current.join(', ')})` },
    ...mine
      .filter((v) => !isProduction(v))
      .map((v) => ({ value: v.id, label: `${v.name} v${v.version} · ${stageBadge(v.stage).label}` })),
  ];
  const chosen = mine.find((v) => v.id === value);
  // an unpinned run of a name with no Production version fails in the worker: say so before submit
  const badge =
    chosen || versions === null || production.every(Boolean)
      ? stageBadge(chosen?.stage ?? 'production')
      : { label: 'No Production version', variant: 'warning' as const };

  return (
    <div className="flex flex-col gap-1.5">
      <Select
        label="Model version"
        options={options}
        value={chosen ? chosen.id : ''}
        onChange={(e) => onChange(e.target.value || null)}
        disabled={disabled || versions === null}
      />
      <div className="flex items-center gap-2 text-xs text-ink-2">
        <StatusBadge status={badge.label} variant={badge.variant} size="sm" />
        {chosen && <span>Pinned for this run</span>}
      </div>
    </div>
  );
}
