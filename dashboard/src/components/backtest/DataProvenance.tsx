import { AlertTriangle } from 'lucide-react';
import { DataStateBadge } from '@/components/ui/DataStateBadge';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { stageBadge } from '@/lib/research';

/** One registry version a research job served (`provenance.ml_models`). */
export interface MlModelProvenance {
  id: string;
  name: string;
  version: number;
  stage: string;
  pinned: boolean;
}

/** Which stored bars a backtest ran on, as returned by the API. */
export interface DataProvenance {
  exchange: string | null;
  symbol: string | null;
  timeframe: string | null;
  bar_count: number | null;
  first_ts: string | null;
  last_ts: string | null;
  gap_count: number | null;
  requested_start?: string | null;
  requested_end?: string | null;
  data_source: string | null;
  /** Job-row provenance only (hyperopt/WF reads); result reads never carry them. */
  hydra_version?: string | null;
  config_hash?: string | null;
  /** The model versions the job served; a backtest result reads them from its job. */
  ml_models?: MlModelProvenance[] | null;
}

const TIMEFRAME_MS: Record<string, number> = {
  '1m': 60_000,
  '5m': 300_000,
  '15m': 900_000,
  '1h': 3_600_000,
  '4h': 14_400_000,
  '1d': 86_400_000,
  '1w': 604_800_000,
};

export function hasRecordedProvenance(p: DataProvenance | null | undefined): p is DataProvenance {
  return p?.data_source === 'db';
}

/** Warnings a reader should see before trusting the result. */
export function provenanceWarnings(p: DataProvenance): string[] {
  const warnings: string[] = [];
  if (p.gap_count && p.gap_count > 0) {
    warnings.push(`${p.gap_count} gap${p.gap_count === 1 ? '' : 's'} in the stored bars`);
  }
  const step = p.timeframe ? TIMEFRAME_MS[p.timeframe] : undefined;
  if (step && p.first_ts && p.requested_start) {
    if (Date.parse(p.first_ts) - Date.parse(p.requested_start) > step) {
      warnings.push(`Data starts at ${formatUtc(p.first_ts)}, after the requested start`);
    }
  }
  if (step && p.last_ts && p.requested_end) {
    if (Date.parse(p.requested_end) - Date.parse(p.last_ts) > step) {
      warnings.push(`Data ends at ${formatUtc(p.last_ts)}, before the requested end`);
    }
  }
  return warnings;
}

/** Differences that make two results not directly comparable. */
export function provenanceMismatch(
  a: DataProvenance | null | undefined,
  b: DataProvenance | null | undefined,
): string[] {
  if (!hasRecordedProvenance(a) || !hasRecordedProvenance(b)) return [];
  const fields: Array<[keyof DataProvenance, string]> = [
    ['exchange', 'exchange'],
    ['symbol', 'symbol'],
    ['timeframe', 'timeframe'],
  ];
  return fields
    .filter(([key]) => a[key] !== b[key])
    .map(([key, label]) => `${label}: ${String(a[key])} vs ${String(b[key])}`);
}

export function formatUtc(iso: string | null | undefined): string {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  return `${new Date(ms).toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

/** "DB" for results run on stored bars; UNKNOWN when no provenance was recorded. */
export function ProvenanceBadge({ provenance }: { provenance: DataProvenance | null | undefined }) {
  if (hasRecordedProvenance(provenance)) {
    const where = `${provenance.exchange} ${provenance.symbol} ${provenance.timeframe}`;
    return (
      <span data-state="db" title={`${provenance.bar_count} stored bars, ${where}`} className="inline-flex">
        <StatusBadge status="DB" variant="success" size="sm" />
      </span>
    );
  }
  return <DataStateBadge state="unknown" reason="No data provenance recorded for this result" />;
}

/** First 12 chars on screen; the full value rides along in the title. */
const short = (v: string) => (v.length > 12 ? `${v.slice(0, 12)}…` : v);

/** The exact stored bars behind a result, plus any coverage warnings. */
export function ProvenancePanel({ provenance }: { provenance: DataProvenance | null | undefined }) {
  if (!hasRecordedProvenance(provenance)) {
    return (
      <div className="flex items-center gap-3 rounded-lg border border-line bg-caution-soft px-4 py-3">
        <AlertTriangle className="h-5 w-5 shrink-0 text-caution" aria-hidden="true" />
        <p className="text-sm text-ink-1">
          No data provenance recorded: this result cannot be traced to stored market data.
        </p>
      </div>
    );
  }
  const warnings = provenanceWarnings(provenance);
  const rows: Array<[string, string]> = [
    ['Source', `${provenance.exchange} ${provenance.symbol} ${provenance.timeframe}`],
    ['Bars', String(provenance.bar_count)],
    ['First bar', formatUtc(provenance.first_ts)],
    ['Last bar', formatUtc(provenance.last_ts)],
    ['Gaps', String(provenance.gap_count ?? 0)],
  ];
  return (
    <div className="rounded-lg border border-line bg-surface px-4 py-3" data-testid="provenance-panel">
      <div className="mb-2 flex items-center gap-2">
        <p className="text-xs font-medium text-ink-2">Data provenance</p>
        <ProvenanceBadge provenance={provenance} />
      </div>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-5">
        {rows.map(([label, value]) => (
          <div key={label} className="flex flex-col">
            <dt className="text-ink-2">{label}</dt>
            <dd className="font-mono text-ink-1">{value}</dd>
          </div>
        ))}
      </dl>
      {(provenance.hydra_version || provenance.config_hash) && (
        <p className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-ink-2">
          {provenance.hydra_version && (
            <span title={provenance.hydra_version}>Hydra {short(provenance.hydra_version)}</span>
          )}
          {provenance.config_hash && (
            <span title={provenance.config_hash}>Config {short(provenance.config_hash)}</span>
          )}
        </p>
      )}
      {provenance.ml_models && provenance.ml_models.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1 text-xs" aria-label="Models served">
          {provenance.ml_models.map((m) => {
            const stage = stageBadge(m.stage);
            return (
              <li key={m.id} title={m.id} className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-ink-1">Model {m.name} v{m.version}</span>
                <StatusBadge status={stage.label} variant={stage.variant} size="sm" />
                {m.pinned && <span className="text-ink-2">Pinned for this run</span>}
              </li>
            );
          })}
        </ul>
      )}
      {warnings.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1">
          {warnings.map((w) => (
            <li key={w} className="flex items-center gap-1.5 text-xs text-caution">
              <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
              {w}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
