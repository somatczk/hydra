import { describe, expect, it } from 'vitest';
import { ApiError } from './api';
import {
  breakerResetOutcome,
  copyForCode,
  killSwitchOutcome,
  ledgerResetOutcome,
  limitsOutcome,
  quickTradeOutcome,
  releaseOutcome,
  saveAgainOutcome,
  startOutcome,
  stopOutcome,
  type KillSwitchResponse,
  type Outcome,
  type QuickTradeRequest,
  type QuickTradeResponse,
} from './outcomes';
import { parseErrorDetail } from './apiState';
import { breaker, session } from '@/test/fixtures';

const NOW = Date.parse('2026-10-21T14:03:00Z');
const text = (o: Outcome) => [o.title, ...o.lines].join(' ');

/** The shared envelope from `api_error` (routes/_db.py:42-54): extras sit beside code/message/state. */
const envelope = (status: number, code: string, message: string, state: string, extra: Record<string, unknown> = {}) =>
  new ApiError(status, JSON.stringify({ detail: { code, message, state, ...extra } }));

/** activate_kill_switch body (trading.py:388-422). */
const ks = (over: Partial<KillSwitchResponse> = {}): KillSwitchResponse => ({
  active: true,
  message: 'Kill switch activated - all positions closed and trading halted',
  sessions_stopped: 2,
  positions_remaining: 0,
  persisted: true,
  flatten_failures: [],
  ...over,
});

const stoppedHolder = session({
  session_id: 'h0000000-1',
  strategy_name: 'RSI mean-revert 15m',
  status: 'stopped',
  flatten_incomplete: true,
  positions_at_stop: 1,
});

describe('killSwitchOutcome', () => {
  it('R-P0-1: a clean response with a stopped holder in the fresh list is C-KS-2b, never "all closed" (case 2)', () => {
    const o = killSwitchOutcome({ ok: ks({ sessions_stopped: 0 }) }, [session({ status: 'stopped' }), stoppedHolder], NOW);
    expect(o.title).toBe('Kill switch on.');
    expect(o.lines).toEqual([
      'No running session to stop at 14:03:00 UTC.',
      'Still held in 1 stopped session: RSI mean-revert 15m. It still counts toward drawdown.',
    ]);
    expect(text(o)).not.toContain('No positions are held');
    expect(text(o)).not.toContain('All positions closed');
    expect(o.actions).toEqual([
      { kind: 'open', label: 'Open RSI mean-revert 15m', href: '/trading/h0000000-1' },
      { kind: 'try_closing', label: 'Try closing again', sessionIds: ['h0000000-1'] },
    ]);
  });

  it('C-KS-2 only when the response and the fresh list are both clean', () => {
    const o = killSwitchOutcome({ ok: ks() }, [session({ status: 'stopped' })], NOW);
    expect(o.tone).toBe('none');
    expect(text(o)).toBe(
      'Kill switch on. 2 sessions stopped at 14:03:00 UTC. No positions are held. New orders are blocked until you release it on Risk.',
    );
  });

  it('C-KS-2c when the fresh list could not be read', () => {
    const o = killSwitchOutcome({ ok: ks() }, new Error('503'), NOW);
    expect(text(o)).toContain("Couldn't check stopped sessions for held positions.");
    expect(text(o)).not.toContain('No positions');
  });

  it('C-KS-3 lists every flatten failure verbatim (session_manager.py:315-320)', () => {
    const failures = ['a1b2c3d4: 1 position(s) remain (BTCUSDT: no mark)'];
    const erroredHolder = session({ session_id: 'a1b2c3d4', status: 'error', positions_at_stop: 1, flatten_incomplete: true });
    const o = killSwitchOutcome({ ok: ks({ positions_remaining: 1, flatten_failures: failures }) }, [erroredHolder], NOW);
    expect(o.tone).toBe('danger');
    expect(o.title).toBe('Kill switch on. 1 position could not be closed.');
    expect(o.lines).toContain(`Reason: ${failures[0]}`);
    expect(o.actions.map((a) => a.kind)).toEqual(['open', 'try_closing']);
  });

  it('C-KS-4 reads the 503 extras and derives the positions line the same way', () => {
    const err = envelope(503, 'kill_switch_not_persisted', 'latched; in memory only', 'unavailable', {
      active: true,
      sessions_stopped: 1,
      positions_remaining: 0,
      persisted: false,
      flatten_failures: [],
    });
    const o = killSwitchOutcome({ err }, [session({ status: 'stopped' })], NOW);
    expect(o.title).toBe('Kill switch on, but not saved.');
    expect(o.lines).toContain('1 session stopped at 14:03:00 UTC. No positions are held. New orders are blocked until you release it on Risk.');
    expect(o.actions[0]).toEqual({ kind: 'save_again', label: 'Save again' });
  });

  it('C-KS-7 on a direct 401, C-KS-5 on a network error, a 502 and engine_not_ready', () => {
    expect(killSwitchOutcome({ err: new ApiError(401, '<html>Authelia</html>') }, [], NOW).title).toBe(
      "Not sent: you're signed out.",
    );
    for (const err of [
      new TypeError('Failed to fetch'),
      new ApiError(502, 'Bad Gateway'),
      envelope(503, 'engine_not_ready', 'Session manager not initialized', 'unavailable'),
    ]) {
      const o = killSwitchOutcome({ err }, [], NOW);
      expect(o.title).toBe('Not confirmed.');
      expect(o.lines).toEqual([
        "We couldn't reach the engine, so we don't know whether trading stopped. Nothing on this screen has changed.",
      ]);
    }
  });
});

describe('saveAgainOutcome', () => {
  it('C-KS-4-SAVED makes no positions claim even when the re-POST stopped nothing (case 3)', () => {
    const o = saveAgainOutcome({ ok: ks({ sessions_stopped: 0 }) });
    expect(text(o)).toBe('Kill switch saved. It stays on after a restart.');
    expect(text(o)).not.toMatch(/position/i);
  });

  it('a second 503 renders C-KS-4 again', () => {
    const err = envelope(503, 'kill_switch_not_persisted', 'x', 'unavailable', { positions_remaining: 0 });
    expect(saveAgainOutcome({ err }).title).toBe('Kill switch on, but not saved.');
  });
});

describe('stopOutcome', () => {
  it('C-STOP-2 when nothing is left', () => {
    expect(text(stopOutcome({ ok: session({ status: 'stopped' }) }, NOW))).toBe('Stopped at 14:03:00 UTC. All positions closed.');
  });

  it('C-STOP-3 offers Try closing again for this session', () => {
    const o = stopOutcome({ ok: session({ status: 'stopped', positions_at_stop: 1, flatten_incomplete: true }) }, NOW);
    expect(o.title).toBe('Stopped, but 1 position is still held.');
    expect(o.actions).toEqual([{ kind: 'try_closing', label: 'Try closing again', sessionIds: [session().session_id] }]);
  });

  it('C-STOP-4 quotes session_stop_not_persisted verbatim (errors.py:60, trading.py:341-347)', () => {
    const msg = 'Session x stopped, but the database still says running: pool closed';
    const o = stopOutcome({ err: envelope(503, 'session_stop_not_persisted', msg, 'unavailable') }, NOW);
    expect(o.title).toBe('Stopped, but not saved.');
    expect(o.lines[0]).toBe(`${msg}. After a restart it will show as running until this is saved.`);
  });

  it('404 says the session is gone; a network error is not confirmed', () => {
    const gone = envelope(404, 'session_not_found', 'Session x not found', 'not_found');
    expect(stopOutcome({ err: gone }).title).toBe('This session no longer exists.');
    expect(stopOutcome({ err: new TypeError('Failed to fetch') }).actions).toEqual([{ kind: 'retry', label: 'Try again' }]);
  });
});

describe('quickTradeOutcome', () => {
  const SID = session().session_id;
  const req: QuickTradeRequest = { symbol: 'BTCUSDT', side: 'buy', order_type: 'MARKET', quantity: '0.0015', session_id: SID };
  /** quick_trade return (trading.py:503-517): side is Side.value, so upper case. */
  const res = (over: Partial<QuickTradeResponse> = {}): QuickTradeResponse => ({
    order_id: 'a1b2c3d4e5f6',
    session_id: SID,
    symbol: 'BTCUSDT',
    side: 'BUY',
    order_type: 'MARKET',
    quantity: 0.0015,
    price: null,
    status: 'FILLED',
    protection: 'armed',
    protective_order_id: 'stop-1',
    exit_orders_deferred: false,
    fill_price: 67612.4,
    mark_source: 'live',
    ...over,
  });

  it('a default stop names no price, because the response has none (case 8)', () => {
    const o = quickTradeOutcome(req, { ok: res() }, SID);
    expect(o.title).toBe('Bought 0.00150 BTC at 67,612.40 (paper).');
    const stopLine = o.lines.find((l) => l.startsWith('Stop placed')) ?? '';
    expect(stopLine).toBe('Stop placed (strategy default).');
    expect(stopLine).not.toMatch(/\d/);
  });

  it('a typed stop is named as requested, since a rejected one falls back to the default (protective_orders.py:255-275)', () => {
    const o = quickTradeOutcome({ ...req, stop_loss: '64256' }, { ok: res({ quantity: 0.001 }) }, SID);
    expect(o.title).toBe('Bought 0.00100 BTC at 67,612.40 (paper).');
    expect(o.lines.slice(1, 3)).toEqual([
      'Stop requested at 64,256.00.',
      "If the engine rejected it, the strategy default stop is in place instead; the session's protection failures say so.",
    ]);
    expect(text(o)).not.toContain('Stop placed');
    const trailing = quickTradeOutcome({ ...req, trailing_stop_pct: '2.5' }, { ok: res() }, SID);
    expect(trailing.lines[1]).toBe('Trailing stop requested (2.5%).');
  });

  it('a filled buy whose stop could not be armed is danger, never "yet" (session_wiring.py:365-368)', () => {
    const o = quickTradeOutcome(req, { ok: res({ protection: 'pending', protective_order_id: null, exit_orders_deferred: true }) }, SID);
    expect(o.tone).toBe('danger');
    expect(o.title).toBe('Bought 0.00150 BTC at 67,612.40 (paper), but no stop could be placed.');
    expect(text(o)).not.toContain('yet');
    expect(o.actions).toEqual([{ kind: 'open', label: 'Open session', href: `/trading/${SID}` }]);
  });

  it('NOOP and terminal statuses say no order was placed (session_wiring.py:591, core/types.py:73-82)', () => {
    const noop = quickTradeOutcome({ ...req, side: 'sell' }, { ok: res({ order_id: null, side: 'SELL', status: 'NOOP', protection: 'none', fill_price: null }) }, SID);
    expect(noop).toEqual({ tone: 'caution', title: 'No order was placed (NOOP).', lines: ['No position was left to reduce.'], actions: [] });
    expect(quickTradeOutcome(req, { ok: res({ status: 'REJECTED' }) }, SID).title).toBe('No order was placed (REJECTED).');
    const partial = quickTradeOutcome(req, { ok: res({ status: 'PARTIALLY_FILLED', order_type: 'LIMIT' }) }, SID);
    expect(partial.title).toBe('Limit order placed (paper).');
  });

  it('flags a session mismatch', () => {
    const o = quickTradeOutcome(req, { ok: res({ session_id: 'other-session' }) }, SID);
    expect(text(o)).toBe('The engine answered for a different session (other-session). Check positions before trading again.');
  });

  it('a deferred limit, a stale mark and a protection:none sell', () => {
    const limit = quickTradeOutcome(req, { ok: res({ status: 'SUBMITTED', order_type: 'LIMIT', protection: 'pending', exit_orders_deferred: true }) }, SID);
    expect(text(limit)).toBe('Limit order placed (paper). The stop will be placed when it fills. Order a1b2c3d4.');
    expect(quickTradeOutcome(req, { ok: res({ mark_source: 'last_close' }) }, SID).tone).toBe('caution');
    const sell = quickTradeOutcome({ ...req, side: 'sell' }, { ok: res({ side: 'SELL', protection: 'none', fill_price: 67590.1 }) }, SID);
    expect(sell.title).toBe('Sold 0.00150 BTC at 67,590.10 (paper).');
    expect(sell.lines).toContain('This reduces the held position; no stop is needed.');
  });

  it('maps quick-trade errors (trading.py:479-492)', () => {
    const rejected = envelope(422, 'risk_rejected', 'Daily loss limit reached', 'invalid', { check: 'daily_loss', intent: 'entry' });
    expect(quickTradeOutcome(req, { err: rejected }, SID)).toMatchObject({
      title: 'Not placed: Daily loss limit reached (daily_loss).',
      actions: [{ kind: 'open', label: 'See limits', href: '/risk#limits' }],
    });
    const short = envelope(422, 'spot_short', 'no BTCUSDT held: a spot SELL cannot open a short', 'invalid');
    expect(quickTradeOutcome(req, { err: short }, SID)).toMatchObject({ inline: true, title: 'no BTCUSDT held: a spot SELL cannot open a short' });
    const halted = envelope(503, 'paper_session_halted', 'session x ledger is halted', 'unavailable');
    expect(text(quickTradeOutcome(req, { err: halted }, SID))).toBe('session x ledger is halted No order was placed.');
    expect(quickTradeOutcome(req, { err: new TypeError('Failed to fetch') }, SID).actions).toEqual([]);
  });
});

describe('startOutcome and copyForCode', () => {
  it('states a silently clamped capital (case 7)', () => {
    const created = session({ paper_capital: 6000, started_at: '2026-10-21T14:02:00+00:00' });
    const o = startOutcome({ strategy_id: 'sma-cross-btc-1h', trading_mode: 'paper', paper_capital: '8000' }, { ok: created }, NOW);
    expect(o.navigate).toBe(`/trading/${created.session_id}`);
    expect(o.lines).toContain('Asked 8,000.00; started with 6,000.00 (all that was unallocated).');
  });

  it('corrects invalid_session for capital (session_manager.py:573-577)', () => {
    const err = envelope(400, 'invalid_session', 'Insufficient paper capital: account=10000, allocated=10000, requested=None', 'invalid');
    expect(copyForCode('invalid_session', parseErrorDetail(err))).toBe(
      'No unallocated paper capital. Stop a session or raise paper capital in Settings. Nothing was started.',
    );
  });

  it('uses the real execution error codes (errors.py:18,36) and insufficient_history extras', () => {
    const dup = envelope(409, 'session_already_running', 'already running', 'invalid');
    expect(startOutcome({ strategy_id: 's', trading_mode: 'paper' }, { err: dup })).toMatchObject({
      title: 'This strategy already has a running session.',
      actions: [{ kind: 'open', label: 'Open it', href: '/trading' }],
    });
    const hist = envelope(422, 'insufficient_history', 'x', 'no_data', { symbol: 'BTCUSDT', timeframe: '1h', have: 120, need: 200 });
    expect(startOutcome({ strategy_id: 's', trading_mode: 'paper' }, { err: hist }).title).toBe(
      'Not enough stored bars to start: have 120 of 200 1h bars for BTCUSDT. Nothing was started.',
    );
    expect(copyForCode('something_new', { code: 'something_new', message: 'Server says no', state: 'invalid' })).toBe('Server says no');
  });
});

describe('risk outcomes', () => {
  it('release: C-REL-2 and C-REL-3 (trading.py:425-445)', () => {
    expect(text(releaseOutcome({ ok: ks({ active: false, sessions_stopped: 0 }) }, NOW))).toBe(
      'Kill switch released at 14:03:00 UTC. Trading can resume. No sessions were restarted.',
    );
    const failed = envelope(503, 'kill_switch_release_failed', 'The release could not be persisted', 'unavailable');
    expect(releaseOutcome({ err: failed }).title).toBe('Not released.');
  });

  it('breaker reset: success, tier_not_active with active_tier from the extras (risk.py:345-352)', () => {
    const done = breakerResetOutcome(3, { ok: [breaker({ current_value: 0 })] }, true, NOW);
    expect(text(done)).toBe('Breaker tier 3 reset at 14:03:00 UTC. Drawdown 0.00%. The kill switch is still on.');
    const stale = envelope(409, 'tier_not_active', 'tier 3 is not active', 'invalid', { active_tier: 2 });
    expect(breakerResetOutcome(3, { err: stale }, false).title).toBe(
      "Tier 3 isn't active any more; the active tier is 2. Nothing was reset.",
    );
    const audit = envelope(503, 'audit_unavailable', 'The reset could not be written to the audit log; nothing changed', 'unavailable');
    expect(breakerResetOutcome(3, { err: audit }, false).title).toBe('The reset could not be written to the audit log; nothing changed');
  });

  it('ledger reset and limits', () => {
    expect(text(ledgerResetOutcome({ ok: { current_drawdown: 0, equity: 9512.4 } }, NOW))).toBe(
      'Paper ledger reset at 14:03:00 UTC. Drawdown 0.00%.',
    );
    expect(ledgerResetOutcome({ err: envelope(503, 'paper_reset_unavailable', 'x', 'unavailable') }).title).toBe('Not reset.');
    expect(limitsOutcome({ err: envelope(503, 'risk_reload_failed', 'x', 'unavailable') }).title).toBe('Saved, but not in force.');
    expect(limitsOutcome({ ok: {} }).title).toBe('Limits saved.');
  });
});
