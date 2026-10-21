/** The four answers as pure functions of polled sources (06 C-ANS-*); `value: null` means loading. */

import type { DataState, PollState } from '@/lib/apiState';
import { duration, pct, utc } from '@/lib/format';
import {
  dataAnswer,
  plural,
  type DataAnswer,
  type SessionDetail,
  type LiveStatus,
  type NeedsYou,
  type SessionResponse,
  type Tone,
} from '@/lib/derive';

export interface Answer {
  value: string | null;
  tone: Tone;
  state: DataState;
  sub?: string;
}

export interface Src<T> {
  data: T | null;
  state: PollState;
}

export interface RiskStatusLite {
  current_drawdown: number;
  max_drawdown_limit: number;
}

export interface ReadinessLite {
  stale: boolean;
  checks: { feeds?: { ok: boolean; streams?: Array<{ ok: boolean }> } };
}

const loading = (s: PollState) => s === 'loading';
export const badgeState = (s: PollState): DataState => (s === 'loading' || s === 'refreshing' ? 'ok' : s);
const LOADING: Answer = { value: null, tone: 'none', state: 'ok' };
const unknown = (value: string): Answer => ({ value, tone: 'caution', state: 'unknown' });

export function runningAnswer(sessions: Src<SessionResponse[]>): Answer {
  if (!sessions.data) return loading(sessions.state) ? LOADING : unknown("Couldn't read sessions");
  const n = sessions.data.filter((s) => s.status === 'running').length;
  return { value: n === 0 ? 'Nothing running' : `${plural(n, 'session')} running`, tone: 'none', state: badgeState(sessions.state) };
}

function blockedReason(r: NonNullable<LiveStatus['circuit_breaker_restrictions']>, now: number): string {
  if (r.daily_halt_until) return `daily loss halt until ${utc(r.daily_halt_until, now)}`;
  if (r.consecutive_pause_until) return `loss-streak pause until ${utc(r.consecutive_pause_until, now)}`;
  return `breaker tier ${r.active_tier}`;
}

export function safeAnswer(live: Src<LiveStatus>, risk: Src<RiskStatusLite>, now: number): Answer {
  if (!live.data) return loading(live.state) ? LOADING : unknown('Kill switch state unknown');
  const ks = { active: 'Kill switch ON', inactive: 'Kill switch off', unknown: 'Kill switch state unknown' }[live.data.kill_switch_state];
  const state = badgeState(live.state);
  const r = live.data.circuit_breaker_restrictions;
  const tier = r ? `Tier ${r.active_tier}` : 'Breakers UNAVAILABLE';
  if (!risk.data && !loading(risk.state)) {
    return {
      value: "Risk state couldn't be loaded. Opening orders are refused until it recovers.",
      tone: live.data.kill_switch_state === 'active' ? 'danger' : 'caution',
      state: 'unavailable',
      sub: ks,
    };
  }
  const dd = risk.data ? `Drawdown ${pct(risk.data.current_drawdown)} of ${pct(risk.data.max_drawdown_limit)}` : 'Drawdown loading';
  if (live.data.kill_switch_state === 'active') {
    return { value: ks, tone: 'danger', state, sub: [tier, dd].join(' · ') };
  }
  const tone: Tone = !r || live.data.kill_switch_state === 'unknown' ? 'caution' : 'none';
  if (r && !r.can_open_new_trades) {
    return { value: `New entries blocked: ${blockedReason(r, now)}`, tone: r.active_tier >= 3 ? 'danger' : 'caution', state, sub: [ks, tier, dd].join(' · ') };
  }
  const parts = [ks, tier, dd, ...(r ? ['New entries allowed'] : [])];
  return { value: parts.join(' · '), tone, state: live.data.kill_switch_state === 'unknown' ? 'unknown' : state };
}

function dataText(a: DataAnswer): Answer {
  switch (a.kind) {
    case 'none':
      return { value: "No session running, so the feed isn't checked", tone: 'none', state: 'ok' };
    case 'unknown':
      return { value: "Couldn't read session health", tone: 'caution', state: 'unknown' };
    case 'late': {
      const w = a.worst;
      const paused = w.paused ? ' · entries paused' : '';
      return {
        value: `${a.late} of ${a.total} late: ${w.name} ${w.tf} bar ${duration(w.overdueMs)} overdue${paused}`,
        tone: 'caution',
        state: 'ok',
      };
    }
    case 'warming':
      return { value: `${a.name} is warming up: no closed ${a.tf} bar yet`, tone: 'none', state: 'ok' };
    case 'ok': {
      const caution = a.unreadable > 0;
      if (a.paused.length > 0) {
        return { value: `Entries paused in ${plural(a.paused.length, 'session')}: ${a.paused[0]}`, tone: 'caution', state: 'ok' };
      }
      return { value: `On time · ${a.total - a.unreadable} of ${a.total} sessions${caution ? ` (${a.unreadable} unreadable)` : ''}`, tone: caution ? 'caution' : 'none', state: 'ok' };
    }
  }
}

/** Loading until the first detail round settles (or nothing runs); never "on time" from unread details. */
export function dataView(sessions: Src<SessionResponse[]>, details: Map<string, SessionDetail | Error>, ready: boolean, now: number): Answer {
  if (!sessions.data) return loading(sessions.state) ? LOADING : unknown("Couldn't read session health");
  if (!ready && sessions.data.some((s) => s.status === 'running')) return LOADING;
  return dataText(dataAnswer(sessions.data, details, now));
}

export function feedSub(r: Src<ReadinessLite>): string {
  if (!r.data || r.data.stale) return "Per session. Full feed check isn't available.";
  const feeds = r.data.checks.feeds;
  if (!feeds) return 'Per session.';
  if (feeds.ok) return 'Per session. Feed check passed.';
  const streams = feeds.streams ?? [];
  const bad = streams.filter((s) => !s.ok).length;
  return `Per session. Feed check failing: ${bad} of ${streams.length} streams.`;
}

export function needsAnswer(needs: NeedsYou, anyLoading: boolean): Answer {
  if (needs.count === null) {
    if (anyLoading) return LOADING;
    return { value: '?', tone: 'caution', state: 'unknown', sub: `Couldn't check everything: ${needs.unavailable.join(', ')} unavailable` };
  }
  if (needs.count === 0) return { value: 'Nothing needs you', tone: 'none', state: 'ok' };
  const verb = needs.count === 1 ? 'needs' : 'need';
  return { value: `${plural(needs.count, 'thing')} ${verb} you`, tone: 'caution', state: 'ok', sub: needs.items[0]?.text };
}

/** C-NEED-HIST: `ts` within 24 h of `now`; null = unreadable. */
export function recentEvents(events: Array<{ ts: string | null }> | null, now: number): number | null {
  if (!events) return null;
  return events.filter((e) => e.ts !== null && now - Date.parse(e.ts) <= 24 * 3600 * 1000).length;
}
