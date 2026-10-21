/** Pure derivations behind every money and safety sentence (08 §3.2); shapes mirror the API models. */

import { money, pct, utc } from './format';

/** `routes/trading.py` SessionResponse; the optional fields arrive with the A3+ ask. */
export interface SessionResponse {
  session_id: string;
  strategy_id: string;
  strategy_name: string | null;
  trading_mode: string;
  status: string;
  exchange_id: string;
  symbols: string[];
  timeframe: string;
  paper_capital: number | null;
  started_at: string | null;
  stopped_at: string | null;
  error_message: string | null;
  positions_at_stop: number;
  flatten_incomplete: boolean;
  persist_error: string | null;
  last_bar_at?: string | null;
  entries_paused?: string | null;
}

/** A fill: the TradeItem model plus the paper extras (`amount`); `quantity` is 0 until the A9 fix. */
export interface TradeItem {
  id?: string | null;
  symbol?: string | null;
  side?: string | null;
  quantity?: number;
  amount?: number;
  price?: number;
  fee?: number;
  pnl?: number;
  net_pnl?: number | null;
  timestamp?: string | null;
}

export interface PositionItem {
  symbol: string;
  direction: string;
  quantity: number;
  avg_entry_price: number;
  unrealized_pnl: number;
}

export interface SessionDetail extends SessionResponse {
  last_bar_at: string | null;
  entries_paused: string | null;
  protection_failures: string[];
  metrics: Record<string, unknown>;
  positions: PositionItem[];
  trades: TradeItem[];
}

export interface Restrictions {
  active_tier: number;
  can_open_new_trades: boolean;
  size_multiplier: number;
  must_flatten: boolean;
  is_shutdown: boolean;
  daily_halt_until: string | null;
  consecutive_pause_until: string | null;
}

export interface LiveStatus {
  kill_switch_active: boolean | null;
  kill_switch_state: 'active' | 'inactive' | 'unknown';
  running_sessions: number | null;
  circuit_breaker_restrictions: Restrictions | null;
  circuit_breakers_state: 'live' | 'unavailable';
}

export interface CircuitBreaker {
  tier: number;
  label: string;
  threshold: string;
  threshold_pct: number;
  current_value: number;
  status: 'Normal' | 'Warning' | 'Tripped';
  active_since: string | null;
  requires_manual_review: boolean;
}

export interface PlatformConfig {
  trading_mode: string;
  paper_capital: number;
  paper_capital_pending: number | null;
}

export interface StopRule {
  stop_loss_method: string;
  stop_loss_value: number;
}

export type Tone = 'none' | 'caution' | 'danger';

export const sessionName = (s: Pick<SessionResponse, 'strategy_name' | 'strategy_id'>): string =>
  s.strategy_name ?? s.strategy_id;

export const plural = (n: number, word: string): string => `${n} ${word}${n === 1 ? '' : 's'}`;

const TF_UNIT: Record<string, number> = { m: 60, h: 3600, d: 86400, w: 604800 };

export function timeframeSeconds(tf: string): number | null {
  const m = /^(\d+)([mhdw])$/.exec(tf);
  return m ? Number(m[1]) * TF_UNIT[m[2]] : null;
}

/** Same rule as HydraClosedBarOverdue: age > 2 × timeframe + 60 s. Null while warming up. */
export function isOverdue(lastBarAt: string | null, tf: string, now: number): boolean | null {
  const tfs = timeframeSeconds(tf);
  const at = lastBarAt ? Date.parse(lastBarAt) : NaN;
  if (tfs === null || Number.isNaN(at)) return null;
  return now - at > (2 * tfs + 60) * 1000;
}

export type DataAnswer =
  | { kind: 'none' }
  | { kind: 'late'; late: number; total: number; worst: { name: string; tf: string; overdueMs: number; paused: string | null } }
  | { kind: 'unknown'; total: number }
  | { kind: 'warming'; name: string; tf: string; total: number; unreadable: number; paused: string[] }
  | { kind: 'ok'; total: number; unreadable: number; paused: string[] };

/** Worst-of over running sessions; an unreadable detail is never counted as on time. */
export function dataAnswer(
  sessions: SessionResponse[],
  details: Map<string, SessionDetail | Error>,
  now: number,
): DataAnswer {
  const running = sessions.filter((s) => s.status === 'running');
  if (running.length === 0) return { kind: 'none' };
  const late: Array<{ name: string; tf: string; overdueMs: number; paused: string | null }> = [];
  const paused: string[] = [];
  let warming: { name: string; tf: string } | null = null;
  let unreadable = 0;
  for (const s of running) {
    const d = details.get(s.session_id);
    const h = d && !(d instanceof Error) ? d : s.last_bar_at !== undefined ? s : null;
    const tfs = timeframeSeconds(s.timeframe);
    if (!h || tfs === null) {
      unreadable += 1;
      continue;
    }
    const lastBarAt = h.last_bar_at ?? null;
    const overdue = isOverdue(lastBarAt, s.timeframe, now);
    const entriesPaused = h.entries_paused ?? null;
    if (overdue === null) {
      warming ??= { name: sessionName(s), tf: s.timeframe };
    } else if (overdue) {
      const overdueMs = now - Date.parse(lastBarAt as string) - tfs * 1000;
      late.push({ name: sessionName(s), tf: s.timeframe, overdueMs, paused: entriesPaused });
    } else if (entriesPaused) {
      paused.push(entriesPaused);
    }
  }
  if (late.length > 0) {
    const worst = late.reduce((a, b) => (b.overdueMs > a.overdueMs ? b : a));
    return { kind: 'late', late: late.length, total: running.length, worst };
  }
  if (unreadable === running.length) return { kind: 'unknown', total: running.length };
  if (warming) return { kind: 'warming', ...warming, total: running.length, unreadable, paused };
  return { kind: 'ok', total: running.length, unreadable, paused };
}

const holds = (s: SessionResponse) => s.flatten_incomplete || s.positions_at_stop > 0;

/** Stopped sessions still holding: the kill switch won't close these (`_stop_targets`). */
export const stoppedHolders = (sessions: SessionResponse[]): SessionResponse[] =>
  sessions.filter((s) => s.status === 'stopped' && holds(s));

/** Status-agnostic holders in a fresh list after a stop or kill switch (an errored holder stays `error`). */
export const heldAfter = (sessions: SessionResponse[]): SessionResponse[] =>
  sessions.filter((s) => s.status !== 'running' && holds(s));

export const ksTargets = (sessions: SessionResponse[]) => ({
  running: sessions.filter((s) => s.status === 'running'),
  errored: sessions.filter((s) => s.status === 'error'),
});

export interface NeedItem {
  kind: 'error' | 'held' | 'persist_error' | 'breaker' | 'ks_not_saved' | 'capital_pending';
  text: string;
  href: string;
}

export interface NeedsYou {
  items: NeedItem[];
  /** Null when any source couldn't be read: render "?", never 0. */
  count: number | null;
  unavailable: string[];
}

/** Current, self-clearing conditions only (R-P1-4); protection failures are not one. */
export function needsYou(src: {
  sessions: SessionResponse[] | null;
  live: LiveStatus | null;
  config: PlatformConfig | null;
  ksNotSaved: boolean;
}): NeedsYou {
  const unavailable: string[] = [];
  const items: NeedItem[] = [];
  const sessions = src.sessions ?? [];
  if (!src.sessions) unavailable.push('sessions');
  const at = (s: SessionResponse) => `/trading/${s.session_id}`;
  const what = (s: SessionResponse) => (s.positions_at_stop > 0 ? plural(s.positions_at_stop, 'position') : 'positions');
  for (const s of sessions.filter((x) => x.status === 'error')) {
    const held = holds(s) ? `; it still holds ${what(s)}` : '';
    items.push({ kind: 'error', text: `${sessionName(s)} stopped with an error: ${s.error_message ?? 'no message'}${held}`, href: at(s) });
  }
  for (const s of heldAfter(sessions).filter((x) => x.status !== 'error')) {
    items.push({ kind: 'held', text: `${sessionName(s)} still holds ${what(s)} after stopping`, href: at(s) });
  }
  for (const s of sessions.filter((x) => x.persist_error)) {
    items.push({ kind: 'persist_error', text: `${sessionName(s)} isn't being saved: ${s.persist_error}`, href: at(s) });
  }
  const tier = src.live?.circuit_breaker_restrictions?.active_tier;
  if (tier === undefined) unavailable.push('breakers');
  else if (tier >= 3) {
    items.push({ kind: 'breaker', text: `Breaker tier ${tier} needs your review before trading resumes`, href: '/risk#breakers' });
  }
  if (src.ksNotSaved) {
    items.push({ kind: 'ks_not_saved', text: 'Kill switch is on but not saved. A restart would turn it off.', href: '/risk#kill-switch' });
  }
  if (!src.config) unavailable.push('settings');
  else if (src.config.paper_capital_pending !== null) {
    items.push({
      kind: 'capital_pending',
      text: `Paper capital change to ${money(src.config.paper_capital_pending)} USDT waits for a ledger reset`,
      href: '/settings',
    });
  }
  return { items, count: unavailable.length ? null : items.length, unavailable };
}

/** `quantity` defaults to 0 (trading.py TradeItem), so 0 means "not reported", not "nothing". */
export function fillQty(t: TradeItem): number | null {
  if (t.quantity !== undefined && t.quantity > 0) return t.quantity;
  if (t.amount !== undefined && t.amount > 0) return t.amount;
  return null;
}

export function sideLabel(side: string | null | undefined): { text: string; caution: boolean } {
  const s = (side ?? '').toLowerCase();
  if (s === 'long') return { text: 'Held', caution: false };
  if (s === 'buy') return { text: 'Buy', caution: false };
  if (s === 'sell') return { text: 'Sell', caution: false };
  return { text: side ?? '—', caution: true };
}

const oneDp = (v: number) => (Number.isInteger(v) ? v.toFixed(1) : String(v));

export function stopRuleLabel(risk: StopRule | Error | null | undefined): string {
  if (!risk || risk instanceof Error) return "rule couldn't be read";
  const v = risk.stop_loss_value;
  switch (risk.stop_loss_method) {
    case 'fixed_pct':
      return `${v}% below fill`;
    case 'atr':
      return `${oneDp(v)} × ATR, set at fill`;
    case 'trailing_stop':
      return `trailing ${v}%`;
    default:
      return `${risk.stop_loss_method} ${v}`;
  }
}

export function breakerRow(b: CircuitBreaker, activeTier: number, now: number = Date.now()): { text: string; tone: Tone } {
  if (b.status === 'Tripped' && b.tier < activeTier) {
    return { text: `Tripped (included in tier ${activeTier})`, tone: 'none' };
  }
  if (b.status === 'Tripped') {
    const since = utc(b.active_since, now);
    return { text: since ? `Active since ${since}` : 'Active', tone: b.tier >= 3 ? 'danger' : 'caution' };
  }
  if (b.status === 'Warning') {
    return { text: `Near: ${pct(b.current_value)} of ${pct(b.threshold_pct)}`, tone: 'caution' };
  }
  return { text: pct(b.threshold_pct) ?? b.threshold, tone: 'none' };
}

/** Percent of closing fills that won; null when none closed (the API reports 0.0 then). */
export function winRate(trades: TradeItem[]): number | null {
  const closing = trades.filter((t) => t.net_pnl !== null && t.net_pnl !== undefined);
  if (closing.length === 0) return null;
  return (closing.filter((t) => (t.net_pnl as number) > 0).length / closing.length) * 100;
}
