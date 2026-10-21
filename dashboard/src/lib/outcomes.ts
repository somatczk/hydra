/** Money-action outcomes (06 §3, §4, §7) as data for OutcomePanel; no JSX here. */

import { isSignedOut, parseErrorDetail, type ApiErrorDetail } from './apiState';
import { heldAfter, plural, sessionName, type CircuitBreaker, type SessionResponse, type Tone } from './derive';
import { money, pct, price, qty, utc } from './format';

export interface OutcomeAction {
  kind: 'open' | 'retry' | 'try_closing' | 'save_again';
  label: string;
  href?: string;
  /** For try_closing: the sessions to re-send DELETE to (never the kill switch). */
  sessionIds?: string[];
}

export interface Outcome {
  tone: Tone;
  title: string;
  lines: string[];
  actions: OutcomeAction[];
  /** Render next to the field instead of in the outcome panel. */
  inline?: boolean;
  /** Navigate here on success (start session). */
  navigate?: string;
}

export type Result<T> = { ok: T } | { err: unknown };

/** trading.py:134-140 KillSwitchResponse. */
export interface KillSwitchResponse {
  active: boolean;
  message: string;
  sessions_stopped: number;
  positions_remaining: number;
  persisted: boolean;
  flatten_failures: string[];
}

/** trading.py:192-205 QuickTradeResponse: no stop price anywhere. */
export interface QuickTradeResponse {
  order_id: string | null;
  session_id: string;
  symbol: string;
  side: string;
  order_type: string;
  quantity: number;
  price: number | null;
  status: string;
  protection: string;
  protective_order_id: string | null;
  exit_orders_deferred: boolean;
  fill_price: number | null;
  mark_source: string | null;
}

/** trading.py:150-158 QuickTradeRequest as the UI sends it (decimals as strings). */
export interface QuickTradeRequest {
  symbol: string;
  side: 'buy' | 'sell';
  order_type: 'MARKET' | 'LIMIT';
  quantity: string;
  price?: string;
  stop_loss?: string;
  take_profit?: string;
  trailing_stop_pct?: string;
  session_id: string;
}

export interface StartSessionRequest {
  strategy_id: string;
  trading_mode: 'paper';
  paper_capital?: string;
}

/** risk.py:57-69 RiskStatus (percents ×100). */
export interface RiskStatus {
  current_drawdown: number;
  equity: number | null;
}

const ok = (title: string, lines: string[] = [], actions: OutcomeAction[] = []): Outcome => ({
  tone: 'none',
  title,
  lines,
  actions,
});

const retry: OutcomeAction = { kind: 'retry', label: 'Try again' };
const NOT_SIGNED_IN = "Not sent: you're signed out.";

function unreachable(err: unknown): boolean {
  if (!(typeof err === 'object' && err !== null && typeof (err as { status?: unknown }).status === 'number')) return true;
  const status = (err as { status: number }).status;
  return status === 502 || status === 504 || parseErrorDetail(err)?.code === 'engine_not_ready';
}

/** Envelope code → sentence (06 §4.3, §7); unknown codes fall back to the server message. */
export function copyForCode(code: string, detail: ApiErrorDetail | null): string {
  const msg = detail?.message ?? '';
  switch (code) {
    case 'kill_switch_active':
      return 'The kill switch is on. Release it on Risk before starting a session.';
    case 'session_already_running':
      return 'This strategy already has a running session.';
    case 'insufficient_history':
      return `Not enough stored bars to start: have ${detail?.have} of ${detail?.need} ${detail?.timeframe} bars for ${detail?.symbol}. Nothing was started.`;
    case 'invalid_session':
      return msg.startsWith('Insufficient paper capital')
        ? 'No unallocated paper capital. Stop a session or raise paper capital in Settings. Nothing was started.'
        : `${msg} Nothing was started.`;
    case 'paper_capital_unavailable':
      return "Paper capital isn't loaded, so the session can't be sized. Nothing was started. Try again shortly.";
    case 'market_rules_unavailable':
      return "Binance's lot and price rules couldn't be loaded. Nothing was started. Try again.";
    case 'engine_not_ready':
      return "The engine isn't ready yet. Nothing was started. Try again shortly.";
    case 'strategy_not_found':
    case 'strategy_unsupported':
      return `${msg} Nothing was started.`;
    case 'tier_not_active':
      return `Tier ${detail?.tier} isn't active any more; the active tier is ${detail?.active_tier}. Nothing was reset.`;
    default:
      return msg || 'Request failed';
  }
}

function positionsPart(
  stopped: number,
  remaining: number,
  failures: string[],
  fresh: SessionResponse[] | Error,
  at: string,
): { tone: Tone; headline: string | null; lines: string[]; actions: OutcomeAction[] } {
  const stoppedLine = stopped > 0 ? `${plural(stopped, 'session')} stopped at ${at}` : `No running session to stop at ${at}`;
  const holders = fresh instanceof Error ? [] : heldAfter(fresh);
  const names = holders.map(sessionName).join(', ');
  const holderActions: OutcomeAction[] = holders.length
    ? [
        { kind: 'open', label: `Open ${sessionName(holders[0])}`, href: `/trading/${holders[0].session_id}` },
        { kind: 'try_closing', label: 'Try closing again', sessionIds: holders.map((s) => s.session_id) },
      ]
    : [];
  if (remaining > 0) {
    return {
      tone: 'danger',
      headline: `${plural(remaining, 'position')} could not be closed.`,
      lines: [
        `${stoppedLine}.${names ? ` Still held in ${names}.` : ''} The kill switch keeps new orders blocked. It still counts toward drawdown.`,
        ...failures.map((f) => `Reason: ${f}`),
      ],
      actions: holderActions.length ? holderActions : [{ kind: 'open', label: 'Open Sessions', href: '/trading' }],
    };
  }
  if (fresh instanceof Error) {
    return {
      tone: 'caution',
      headline: null,
      lines: [`${stoppedLine}. Couldn't check stopped sessions for held positions.`],
      actions: [{ kind: 'open', label: 'Open Sessions', href: '/trading' }],
    };
  }
  if (holders.length) {
    return {
      tone: 'caution',
      headline: null,
      lines: [
        stopped > 0 ? `${stoppedLine}; they closed all their positions.` : `${stoppedLine}.`,
        `Still held in ${plural(holders.length, 'stopped session')}: ${names}. It still counts toward drawdown.`,
      ],
      actions: holderActions,
    };
  }
  return {
    tone: 'none',
    headline: null,
    lines: [`${stoppedLine}. No positions are held. New orders are blocked until you release it on Risk.`],
    actions: [],
  };
}

function notConfirmed(
  what: string,
  err: unknown,
  next = 'Nothing on this screen has changed.',
  actions: OutcomeAction[] = [retry],
): Outcome {
  const msg = unreachable(err) ? null : parseErrorDetail(err)?.message;
  return {
    tone: 'caution',
    title: 'Not confirmed.',
    lines: [`We couldn't reach the engine, so we don't know whether ${what}. ${next}`, ...(msg ? [msg] : [])],
    actions,
  };
}

const signedOut = (again: string): Outcome => ({
  tone: 'caution',
  title: NOT_SIGNED_IN,
  lines: [`Sign in, then ${again}.`],
  actions: [],
});

const NOT_SAVED_LINE = 'The engine has stopped trading, but the switch is only in memory. If the engine restarts, it will be off.';

/** C-KS-2/2b/2c/3/4/5/7: the response plus a fresh GET /trading/sessions (R-P0-1). */
export function killSwitchOutcome(
  result: Result<KillSwitchResponse>,
  fresh: SessionResponse[] | Error,
  now: number = Date.now(),
): Outcome {
  const at = utc(now, now) ?? '';
  if ('ok' in result) {
    const r = result.ok;
    const p = positionsPart(r.sessions_stopped, r.positions_remaining, r.flatten_failures, fresh, at);
    return {
      tone: p.tone,
      title: p.headline ? `Kill switch on. ${p.headline}` : 'Kill switch on.',
      lines: p.lines,
      actions: p.actions,
    };
  }
  const err = result.err;
  if (isSignedOut(err)) return signedOut('press Stop all again');
  const d = parseErrorDetail(err);
  if (d?.code === 'kill_switch_not_persisted') {
    const p = positionsPart(
      Number(d.sessions_stopped ?? 0),
      Number(d.positions_remaining ?? 0),
      Array.isArray(d.flatten_failures) ? d.flatten_failures.map(String) : [],
      fresh,
      at,
    );
    return {
      tone: 'danger',
      title: 'Kill switch on, but not saved.',
      lines: [NOT_SAVED_LINE, ...(p.headline ? [p.headline] : []), ...p.lines],
      actions: [{ kind: 'save_again', label: 'Save again' }, ...p.actions],
    };
  }
  return notConfirmed('trading stopped', err);
}

/** Re-POST after C-KS-4: a 200 says nothing about holdings, so no positions line. */
export function saveAgainOutcome(result: Result<KillSwitchResponse>): Outcome {
  if ('ok' in result) return ok('Kill switch saved.', ['It stays on after a restart.']);
  const err = result.err;
  if (isSignedOut(err)) return signedOut('press Save again');
  if (parseErrorDetail(err)?.code === 'kill_switch_not_persisted') {
    return { tone: 'danger', title: 'Kill switch on, but not saved.', lines: [NOT_SAVED_LINE], actions: [{ kind: 'save_again', label: 'Save again' }] };
  }
  return notConfirmed('the kill switch was saved', err);
}

/** C-STOP-2/3/4 for DELETE /trading/sessions/{id} (trading.py:325-352). */
export function stopOutcome(result: Result<SessionResponse>, now: number = Date.now()): Outcome {
  if ('ok' in result) {
    const s = result.ok;
    if (!s.flatten_incomplete && s.positions_at_stop === 0) {
      return ok(`Stopped at ${utc(now, now)}.`, ['All positions closed.']);
    }
    const held = s.positions_at_stop > 0 ? `${plural(s.positions_at_stop, 'position')} ${s.positions_at_stop === 1 ? 'is' : 'are'}` : 'positions are';
    return {
      tone: 'danger',
      title: `Stopped, but ${held} still held.`,
      lines: ["They stay on this session's paper ledger and still count toward drawdown and allocated capital."],
      actions: [{ kind: 'try_closing', label: 'Try closing again', sessionIds: [s.session_id] }],
    };
  }
  const err = result.err;
  if (isSignedOut(err)) return signedOut('stop the session again');
  const d = parseErrorDetail(err);
  if (d?.code === 'session_stop_not_persisted') {
    return {
      tone: 'danger',
      title: 'Stopped, but not saved.',
      lines: [`${d.message}. After a restart it will show as running until this is saved.`],
      actions: [retry],
    };
  }
  if (d?.code === 'session_not_found') {
    return { tone: 'caution', title: 'This session no longer exists.', lines: [], actions: [{ kind: 'open', label: 'Open Sessions', href: '/trading' }] };
  }
  return notConfirmed('the session stopped', err);
}

/** OrderStatus (core/types.py:73-82) values that leave a live order; NOOP (session_wiring.py:539,591) placed nothing. */
const PLACED: ReadonlySet<string> = new Set(['PENDING', 'SUBMITTED', 'PARTIALLY_FILLED']);
const FALLBACK_LINE = "If the engine rejected it, the strategy default stop is in place instead; the session's protection failures say so.";

const baseAsset = (symbol: string) => symbol.replace(/\/?USDT$/, '') || symbol;

/** C-QT-2/3/4, the protection:none sell and the session mismatch (trading.py:453-517). */
export function quickTradeOutcome(
  req: QuickTradeRequest,
  result: Result<QuickTradeResponse>,
  selectedSessionId: string,
): Outcome {
  if ('ok' in result) {
    const r = result.ok;
    if (r.session_id !== selectedSessionId) {
      return {
        tone: 'danger',
        title: `The engine answered for a different session (${r.session_id}).`,
        lines: ['Check positions before trading again.'],
        actions: [],
      };
    }
    const base = baseAsset(r.symbol);
    const amount = `${qty(r.quantity)} ${base}`;
    const fill = price(r.fill_price ?? r.price);
    const order = r.order_id ? [`Order ${r.order_id.slice(0, 8)}.`] : [];
    if (PLACED.has(r.status)) {
      const kind = r.order_type === 'LIMIT' ? 'Limit' : 'Market';
      return ok(`${kind} order placed (paper).`, [...(r.exit_orders_deferred ? ['The stop will be placed when it fills.'] : []), ...order]);
    }
    if (r.status !== 'FILLED') {
      const why = r.status === 'NOOP' ? ['No position was left to reduce.'] : [];
      return { tone: 'caution', title: `No order was placed (${r.status}).`, lines: why, actions: [] };
    }
    const mark =
      r.mark_source === 'stale_live'
        ? 'Filled at the last live price; the feed went stale after it.'
        : r.mark_source === 'last_close'
          ? "Filled at the last closed bar's price; no live price was available."
          : 'Filled at the live price.';
    const caution = r.mark_source === 'stale_live' || r.mark_source === 'last_close';
    if (r.side.toLowerCase() === 'sell') {
      const rest = r.protection === 'none' ? 'This reduces the held position; no stop is needed.' : 'The rest of the position keeps its stop.';
      return { tone: caution ? 'caution' : 'none', title: `Sold ${amount} at ${fill} (paper).`, lines: [mark, rest, ...order], actions: [] };
    }
    if (r.protection !== 'armed') {
      return {
        tone: 'danger',
        title: `Bought ${amount} at ${fill} (paper), but no stop could be placed.`,
        lines: [mark, "The engine closes an unprotected position, or stops the session if it cannot. Check this session's positions and fills.", ...order],
        actions: [{ kind: 'open', label: 'Open session', href: `/trading/${r.session_id}` }],
      };
    }
    const typed = req.stop_loss
      ? `Stop requested at ${price(Number(req.stop_loss))}.`
      : req.trailing_stop_pct
        ? `Trailing stop requested (${req.trailing_stop_pct}%).`
        : null;
    const stop = typed ? [typed, FALLBACK_LINE] : ['Stop placed (strategy default).'];
    return {
      tone: caution ? 'caution' : 'none',
      title: `Bought ${amount} at ${fill} (paper).`,
      lines: [mark, ...stop, ...order],
      actions: [],
    };
  }
  const err = result.err;
  if (isSignedOut(err)) return signedOut('place the order again');
  const d = parseErrorDetail(err);
  switch (d?.code) {
    case 'risk_rejected':
      return {
        tone: 'caution',
        title: `Not placed: ${d.message} (${d.check}).`,
        lines: [],
        actions: [{ kind: 'open', label: 'See limits', href: '/risk#limits' }],
      };
    case 'spot_short':
    case 'order_rejected':
    case 'validation_error':
      return { tone: 'danger', title: d.message, lines: [], actions: [], inline: true };
    case 'session_not_running':
      return { tone: 'caution', title: "This session isn't running, so no order was placed.", lines: [], actions: [] };
    case 'paper_state_unpersisted':
    case 'paper_session_halted':
      return { tone: 'danger', title: d.message, lines: ['No order was placed.'], actions: [] };
  }
  return notConfirmed('the order was placed', err, 'Check the fills before trying again.', []);
}

/** POST /trading/sessions: 201 navigates; a silently clamped capital is stated (R-P2-6). */
export function startOutcome(req: StartSessionRequest, result: Result<SessionResponse>, now: number = Date.now()): Outcome {
  if ('ok' in result) {
    const s = result.ok;
    const asked = req.paper_capital !== undefined ? Number(req.paper_capital) : null;
    const clamped =
      asked !== null && s.paper_capital !== null && s.paper_capital < asked
        ? [`Asked ${money(asked)}; started with ${money(s.paper_capital)} (all that was unallocated).`]
        : [];
    return {
      ...ok(`Started ${utc(s.started_at ?? now, now)}.`, [`Warming up. Waiting for the first closed ${s.timeframe} bar.`, ...clamped]),
      navigate: `/trading/${s.session_id}`,
    };
  }
  const err = result.err;
  if (isSignedOut(err)) return signedOut('start the session again');
  const d = parseErrorDetail(err);
  if (!d || unreachable(err)) return notConfirmed('the session started', err);
  const actions: OutcomeAction[] =
    d.code === 'kill_switch_active'
      ? [{ kind: 'open', label: 'Release on Risk', href: '/risk#kill-switch' }]
      : d.code === 'session_already_running'
        ? [{ kind: 'open', label: 'Open it', href: '/trading' }]
        : [];
  return { tone: 'caution', title: copyForCode(d.code, d), lines: [], actions };
}

/** DELETE /trading/kill-switch (C-REL-2/3). */
export function releaseOutcome(result: Result<KillSwitchResponse>, now: number = Date.now()): Outcome {
  if ('ok' in result) return ok(`Kill switch released at ${utc(now, now)}.`, ['Trading can resume. No sessions were restarted.']);
  const err = result.err;
  if (isSignedOut(err)) return signedOut('release again');
  const d = parseErrorDetail(err);
  if (d?.state === 'unavailable' && !unreachable(err)) {
    return {
      tone: 'danger',
      title: 'Not released.',
      lines: ["The release couldn't be saved, so the kill switch stays on. Your reason is kept. Try again."],
      actions: [retry],
    };
  }
  if (d && !unreachable(err)) return { tone: 'danger', title: d.message, lines: [], actions: [], inline: true };
  return notConfirmed('the kill switch was released', err);
}

/** POST /risk/circuit-breakers/{tier}/reset (risk.py:330-375). */
export function breakerResetOutcome(
  tier: number,
  result: Result<CircuitBreaker[]>,
  killSwitchOn: boolean,
  now: number = Date.now(),
): Outcome {
  if ('ok' in result) {
    const dd = pct(result.ok[0]?.current_value);
    return ok(`Breaker tier ${tier} reset at ${utc(now, now)}.`, [
      ...(dd ? [`Drawdown ${dd}.`] : []),
      ...(killSwitchOn ? ['The kill switch is still on.'] : []),
    ]);
  }
  const err = result.err;
  if (isSignedOut(err)) return signedOut('reset again');
  const d = parseErrorDetail(err);
  if (d?.code === 'tier_not_active') {
    return { tone: 'caution', title: copyForCode(d.code, { ...d, tier }), lines: [], actions: [] };
  }
  if (d?.code === 'confirm_mismatch' || d?.code === 'validation_error') {
    return { tone: 'danger', title: d.message, lines: [], actions: [], inline: true };
  }
  if (d && !unreachable(err)) return { tone: 'danger', title: d.message, lines: [], actions: [retry] };
  return notConfirmed('the breaker was reset', err);
}

/** POST /risk/paper-account/reset (C-RST-2/3). */
export function ledgerResetOutcome(result: Result<RiskStatus>, now: number = Date.now()): Outcome {
  if ('ok' in result) {
    return ok(`Paper ledger reset at ${utc(now, now)}.`, [`Drawdown ${pct(result.ok.current_drawdown)}.`]);
  }
  const err = result.err;
  if (isSignedOut(err)) return signedOut('reset again');
  const d = parseErrorDetail(err);
  if (d?.code === 'paper_reset_unavailable') {
    return {
      tone: 'danger',
      title: 'Not reset.',
      lines: ["Risk state is recovering, a held position has no mark yet, or the activity log couldn't be written. Nothing changed. Try again in a minute."],
      actions: [retry],
    };
  }
  if (d && !unreachable(err)) return { tone: 'danger', title: d.message, lines: [], actions: [] };
  return notConfirmed('the ledger was reset', err);
}

/** PUT /risk/config: 503 risk_reload_failed means saved but not in force (risk.py:495-508). */
export function limitsOutcome(result: Result<unknown>): Outcome {
  if ('ok' in result) return ok('Limits saved.');
  const err = result.err;
  if (isSignedOut(err)) return signedOut('save again');
  const d = parseErrorDetail(err);
  if (d?.code === 'risk_reload_failed') {
    return {
      tone: 'danger',
      title: 'Saved, but not in force.',
      lines: ["The running engine didn't reload the new limits, so the old limits are still enforced."],
      actions: [retry],
    };
  }
  if (d && !unreachable(err)) return { tone: 'danger', title: d.message, lines: [], actions: [], inline: d.state === 'invalid' };
  return notConfirmed('the limits were saved', err);
}
