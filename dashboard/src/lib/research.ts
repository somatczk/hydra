/** Pure derivations behind the research screens (08 §4); shapes mirror the API models. No React. */

import { age, duration, pct } from './format';
import { parseErrorDetail } from './apiState';

export type Tone = 'none' | 'caution' | 'danger';

export type JobKind = 'backtest' | 'hyperopt' | 'walkforward';

/** One durable job: a `GET /api/backtest/jobs` row, or a hyperopt progress row (which carries no kind). */
export interface ResearchJob {
  task_id: string;
  status: string;
  progress?: number | null;
  created_at?: string | null;
  finished_at?: string | null;
  strategy_id?: string | null;
  name?: string | null;
  error?: string | null;
  result_id?: string | null;
  completed_trials?: number | null;
  total_trials?: number | null;
  /** The worker's auto-fetch of missing bars, as the job row's `detail.fetch` carries it. */
  fetch?: JobFetch | null;
}

/** `detail.fetch` (data/on_demand.py `_emit`, worker `_fetch_failed`): a failure before any progress has only state and reason. */
export interface JobFetch {
  state: string;
  ranges?: string[][];
  bars_fetched?: number;
  weight_spent?: number;
  elapsed_ms?: number;
  reason?: string;
}

/** A `GET /api/backtest/strategies/{id}/params` row; the editor collects min/max overrides on top. */
export interface StrategyParamDef {
  name: string;
  label?: string;
  type: string;
  default: number;
  min: number;
  max: number;
  step?: number;
}

/** The wire `param_space` entry (jobs.py ParamDefRequest); categorical never reaches the editor. */
export interface WireParamDef {
  name: string;
  type: string;
  low: number;
  high: number;
}

/** Holdout metrics re-run by the worker (`HyperoptHoldoutResponse`); fractions, not percents. */
export interface HoldoutMetrics {
  sharpe: number;
  total_return: number;
  max_drawdown: number;
  total_trades: number;
}

/** One walk-forward fold (`FoldMetricsResponse`); only the fields the folds table shows. */
export interface WfFold {
  fold_index: number;
  is_start?: string | null;
  is_end?: string | null;
  oos_start?: string | null;
  oos_end?: string | null;
  selected_params?: Record<string, unknown> | null;
  n_trials?: number | null;
}

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DP2 = new Intl.NumberFormat('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const dp2 = (v: number) => DP2.format(v);

/** "9 Sep 2026" — bar coverage is a date fact; the time of day would be noise. */
export function utcDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

function windowText(start: string | null | undefined, end: string | null | undefined): string {
  return start && end ? `${utcDay(start)} to ${utcDay(end)}` : '—';
}

/** Status text for every durable state; the text carries the meaning, colour never does. */
export function jobStatusText(status: string): string {
  switch (status) {
    case 'queued':
      return 'Queued';
    case 'running':
      return 'Running';
    case 'cancelling':
      return 'Cancelling…';
    case 'completed':
      return 'Completed';
    case 'failed':
      return 'Failed';
    case 'cancelled':
      return 'Cancelled';
    default:
      return status;
  }
}

export function jobStatusTone(status: string): Tone {
  if (status === 'failed') return 'danger';
  if (status === 'cancelled' || status === 'cancelling') return 'caution';
  return 'none';
}

/** "Queued/Started 3 min ago" while active; "Finished 1 h ago" from finished_at once terminal. */
export function jobAgeText(
  status: string,
  createdAtIso: string | null | undefined,
  finishedAtIso: string | null | undefined,
  now: number = Date.now(),
): string | null {
  const terminal = TERMINAL.has(status);
  const iso = terminal ? finishedAtIso ?? createdAtIso : createdAtIso;
  const at = iso ? Date.parse(iso) : NaN;
  if (Number.isNaN(at)) return null;
  const ago = age(now - at, now);
  if (ago === null) return null;
  if (terminal) return `Finished ${ago}`;
  return status === 'queued' ? `Queued ${ago}` : `Started ${ago}`;
}

/** Backtests report progress, hyperopt reports trial counts, walk-forward reports neither. */
export function jobProgressText(
  kind: JobKind,
  job: Pick<ResearchJob, 'progress' | 'completed_trials' | 'total_trials'>,
): string | null {
  if (kind === 'backtest') {
    return typeof job.progress === 'number' && Number.isFinite(job.progress)
      ? `${Math.round(job.progress)}%`
      : null;
  }
  if (kind === 'hyperopt') {
    const { completed_trials: done, total_trials: total } = job;
    return typeof done === 'number' && typeof total === 'number'
      ? `${done}/${total} trials`
      : null;
  }
  return null;
}

export interface NoStoredBars {
  title: string;
  line: string;
}

/** C-NOBARS: the 422 no_market_data envelope in operator words; null for anything else. */
export function noStoredBars(err: unknown): NoStoredBars | null {
  const detail = parseErrorDetail(err);
  if (detail?.code !== 'no_market_data') return null;
  const first = detail.available_first_ts;
  const last = detail.available_last_ts;
  const line =
    typeof first === 'string' && typeof last === 'string'
      ? `Stored bars cover ${utcDay(first)} to ${utcDay(last)}.`
      : 'Nothing is stored for this market yet.';
  return {
    title: `No stored bars for ${String(detail.symbol ?? '')} ${String(detail.timeframe ?? '')} in this range. Nothing was simulated.`,
    line,
  };
}

/** The holdout block: window, then the metrics the holdout run actually produced. Null when there was no holdout. */
export function holdoutLines(
  holdout: HoldoutMetrics | null | undefined,
  windows: { holdout_start: string | null | undefined; holdout_end: string | null | undefined },
): string[] | null {
  if (!holdout) return null;
  const lines: string[] = [];
  if (windows.holdout_start && windows.holdout_end) {
    lines.push(`Holdout window: ${windowText(windows.holdout_start, windows.holdout_end)}`);
  }
  lines.push(`Sharpe ${dp2(holdout.sharpe)}`);
  lines.push(`Return ${pct(holdout.total_return * 100)}`);
  lines.push(`Max drawdown ${pct(holdout.max_drawdown * 100)}`);
  lines.push(`Trades ${holdout.total_trades}`);
  return lines;
}

/** 'PBO 0.30 · DSR 0.41 · Holdout PSR 0.62' — only what the API returned, never 0-from-null. */
export function validationLines(v: {
  holdout_psr?: number | null;
  dsr?: number | null;
  pbo?: number | null;
}): string | null {
  const parts: string[] = [];
  if (typeof v.pbo === 'number') parts.push(`PBO ${dp2(v.pbo)}`);
  if (typeof v.dsr === 'number') parts.push(`DSR ${dp2(v.dsr)}`);
  if (typeof v.holdout_psr === 'number') parts.push(`Holdout PSR ${dp2(v.holdout_psr)}`);
  return parts.length > 0 ? parts.join(' · ') : null;
}

/** Trial outcome text; a healthy trial needs no label. */
export function trialStatusText(status: string | null | undefined): string | null {
  if (status === 'failed') return 'Failed';
  if (status === 'no_trades') return 'No trades';
  return null;
}

/** Params to the wire shape the run endpoints require; the editor-only step never crosses. */
export function paramSpaceBody(defs: StrategyParamDef[]): WireParamDef[] {
  return defs.map((d) => ({ name: d.name, type: d.type, low: d.min, high: d.max }));
}

/** Display strings for the walk-forward folds table. */
export function wfFoldRow(fold: WfFold): { isWindow: string; oosWindow: string; params: string } {
  const entries = Object.entries(fold.selected_params ?? {});
  return {
    isWindow: windowText(fold.is_start, fold.is_end),
    oosWindow: windowText(fold.oos_start, fold.oos_end),
    params: entries.length > 0 ? entries.map(([k, v]) => `${k}=${String(v)}`).join(', ') : '—',
  };
}

/** Python's round(): half to even, so the estimate sizes windows exactly as `walkforward_months` does. */
function roundHalfEven(x: number): number {
  const r = Math.round(x);
  return x % 1 === 0.5 && r % 2 !== 0 ? r - 1 : r;
}

/** The walk-forward windows `fold_count` really asks for (jobs.py `walkforward_months` + the walkforward.py loop).
 * fold_count divides the span: each in-sample window is train% of span/k months, each out-of-sample window the
 * rest, and windows roll by one out-of-sample step, so the run makes about (k − train)/(1 − train) folds (4k − 3 at
 * 75 %), not k. An estimate from the requested dates: the worker sizes from the stored bars' span. Null when no
 * window fits or an input is unusable. */
export function wfWindowPlan(
  startDate: string,
  endDate: string,
  divisor: number,
  trainFraction: number,
): { isMonths: number; oosMonths: number; folds: number } | null {
  const days = Math.floor((Date.parse(endDate) - Date.parse(startDate)) / 86_400_000);
  if (!(days > 0) || !(divisor >= 1) || !(trainFraction > 0 && trainFraction < 1)) return null;
  const months = Math.max(1, Math.floor(days / 30.44));
  const isMonths = Math.max(1, roundHalfEven((months * trainFraction) / divisor));
  const oosMonths = Math.max(1, roundHalfEven((months * (1 - trainFraction)) / divisor));
  const windowDays = 30 * (isMonths + oosMonths);
  if (days < windowDays) return null;
  return { isMonths, oosMonths, folds: Math.floor((days - windowDays) / (30 * oosMonths)) + 1 };
}

const MAX_RANGES_SHOWN = 3;

/** The auto-fetch as one sentence: what is being (or was) fetched, how much, the request weight and the time spent. */
export function fetchLine(fetch: JobFetch | null | undefined): string | null {
  if (!fetch) return null;
  const ranges = fetch.ranges ?? [];
  const bars = (fetch.bars_fetched ?? 0).toLocaleString('en-US');
  const spans = ranges.slice(0, MAX_RANGES_SHOWN).map(([a, b]) => `${utcDay(a)} – ${utcDay(b)}`);
  if (ranges.length > MAX_RANGES_SHOWN) spans.push(`${ranges.length - MAX_RANGES_SHOWN} more`);
  const where = ranges.length > 0 ? ` over ${ranges.length === 1 ? '1 range' : `${ranges.length} ranges`} (${spans.join('; ')})` : '';
  const cost = [
    fetch.weight_spent !== undefined ? `weight ${fetch.weight_spent}` : null,
    fetch.elapsed_ms !== undefined ? duration(fetch.elapsed_ms) : null,
  ].filter(Boolean);
  const tail = cost.length > 0 ? ` · ${cost.join(' · ')}` : '';
  if (fetch.state === 'fetching') return `Fetching missing bars: ${bars} so far${where}${tail}`;
  if (fetch.state === 'fetched') {
    return ranges.length === 0 ? 'No missing bars: every bar was already stored' : `Fetched ${bars} missing bars${where} before the run${tail}`;
  }
  if (fetch.state === 'failed') {
    return `Fetching missing bars failed${fetch.reason ? ` (${fetch.reason})` : ''} after ${bars} bars${where}${tail}`;
  }
  return `Auto-fetch ${fetch.state}: ${bars} bars${where}${tail}`;
}
