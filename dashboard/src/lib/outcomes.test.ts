import { describe, expect, it } from 'vitest';
import { ApiError } from './api';
import {
  approvalOutcome,
  breakerResetOutcome,
  copyForCode,
  killSwitchOutcome,
  ledgerResetOutcome,
  limitsOutcome,
  liveGateOutcome,
  liveOrderOutcome,
  liveStartOutcome,
  quickTradeOutcome,
  releaseOutcome,
  saveAgainOutcome,
  startOutcome,
  stopOutcome,
  tryClosingOutcome,
  type ApproveResponse,
  type DenyResponse,
  type KillSwitchResponse,
  type LiveSessionStartRequest,
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

/** activate_kill_switch body (trading.py:434-468). */
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

  it('C-STOP-4 quotes session_stop_not_persisted verbatim (errors.py:60, trading.py:362-368)', () => {
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

describe('tryClosingOutcome', () => {
  const notSaved = envelope(503, 'session_stop_not_persisted', 'Session b stopped, but the database still says running', 'unavailable');

  it('one holder: its retry re-sends only that DELETE', () => {
    const o = tryClosingOutcome([{ id: 'b', name: 'Breakout', result: { err: notSaved } }], NOW);
    expect(o.title).toBe('Stopped, but not saved.');
    expect(o.actions).toEqual([{ kind: 'try_closing', label: 'Try closing again', sessionIds: ['b'] }]);
  });

  it('several holders: worst tone, one line each, retry only the ones that failed', () => {
    const o = tryClosingOutcome(
      [
        { id: 'a', name: 'Momentum 4h', result: { ok: session({ session_id: 'a', status: 'stopped' }) } },
        { id: 'b', name: 'Breakout', result: { err: notSaved } },
      ],
      NOW,
    );
    expect(o.tone).toBe('danger');
    expect(o.title).toBe('Tried closing 2 sessions again.');
    expect(o.lines).toEqual([
      'Momentum 4h: Stopped at 14:03:00 UTC. All positions closed.',
      'Breakout: Stopped, but not saved. Session b stopped, but the database still says running. After a restart it will show as running until this is saved.',
    ]);
    expect(o.actions).toEqual([{ kind: 'try_closing', label: 'Try closing again', sessionIds: ['b'] }]);
  });

  it('several holders all closed: no retry', () => {
    const ok = (id: string) => ({ id, name: id, result: { ok: session({ session_id: id, status: 'stopped' }) } });
    const o = tryClosingOutcome([ok('a'), ok('b')], NOW);
    expect(o.tone).toBe('none');
    expect(o.actions).toEqual([]);
  });
});

describe('quickTradeOutcome', () => {
  const SID = session().session_id;
  const req: QuickTradeRequest = { symbol: 'BTCUSDT', side: 'buy', order_type: 'MARKET', quantity: '0.0015', session_id: SID };
  /** quick_trade return (trading.py:549-563): side is Side.value, so upper case. */
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

  it('maps quick-trade errors (trading.py:525-538)', () => {
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
  it('release: C-REL-2 and C-REL-3 (trading.py:471-491)', () => {
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

describe('approvalOutcome', () => {
  const approved: ApproveResponse = {
    executed: true,
    result: { active: false },
    actor: 'service:mcp',
    approved_by: 'user:soma',
  };
  const denied: DenyResponse = { id: '3f0b9e2a', status: 'denied' };

  it('approve: the double attribution names both actors (F8 trail attribution)', () => {
    const o = approvalOutcome({ ok: approved });
    expect(o.title).toBe('Released by Claude, approved by you.');
    expect(o.lines).toEqual(['Trading can resume. No sessions were restarted.']);
    expect(o.tone).toBe('none');
  });

  it('deny: state, then consequence', () => {
    const o = approvalOutcome({ ok: denied });
    expect(text(o)).toBe('Denied. Claude can ask again.');
  });

  it('wrong code: the frozen sentence inline, with the attempts left (422 code_mismatch)', () => {
    const o = approvalOutcome({ err: envelope(422, 'code_mismatch', "That code didn't match. Nothing changed.", 'invalid', { attempts_left: 4 }) });
    expect(o.inline).toBe(true);
    expect(o.title).toBe("That code didn't match. Nothing changed.");
    expect(o.lines).toEqual(['4 attempts left.']);
    expect(approvalOutcome({ err: envelope(422, 'code_mismatch', "That code didn't match. Nothing changed.", 'invalid', { attempts_left: 1 }) }).lines).toEqual([
      '1 attempt left.',
    ]);
  });

  it('a service principal is refused with the server message inline (403)', () => {
    const o = approvalOutcome({
      err: envelope(403, 'principal_not_allowed', 'Only a signed-in person can approve or deny; service principals cannot', 'forbidden'),
    });
    expect(o.inline).toBe(true);
    expect(o.title).toBe('Only a signed-in person can approve or deny; service principals cannot');
  });

  it('an executor failure says the approved action did not run and asks for a new one', () => {
    const o = approvalOutcome({ err: envelope(503, 'approval_execution_failed', "The approved action 'kill_switch_release' did not run: boom", 'unavailable') });
    expect(o.tone).toBe('danger');
    expect(o.title).toBe("Approved, but it didn't run.");
    expect(o.lines).toEqual(["The approved action 'kill_switch_release' did not run: boom", 'Ask for a new one.']);
  });

  it('a live order executor failure never asks for a new one — the order may exist (R24)', () => {
    const msg = "The approved action 'live_order' did not run: exchange unreachable";
    const o = approvalOutcome({ err: envelope(503, 'approval_execution_failed', msg, 'unavailable') }, 'approve', 'live_order');
    expect(o.tone).toBe('danger');
    expect(o.title).toBe('Not confirmed.');
    expect(o.unanswered).toBe(true);
    expect(o.lines).toEqual(["We don't know whether the order was placed. Check this session's fills and positions before trying again.", msg]);
    expect(o.lines).not.toContain('Ask for a new one.');
  });

  it('a live session start executor failure says the session start is unknown (R24)', () => {
    const o = approvalOutcome(
      { err: envelope(503, 'approval_execution_failed', "The approved action 'live_session_start' did not run: boom", 'unavailable') },
      'approve',
      'live_session_start',
    );
    expect(o.title).toBe('Not confirmed.');
    expect(o.lines[0]).toBe("We don't know whether the session started. Check the sessions list before trying again.");
    expect(text(o)).not.toContain('Ask for a new one.');
  });

  it('a network-lost approve on a live action renders the unknown-order reality, not the code sentence (R24)', () => {
    const o = approvalOutcome({ err: new TypeError('Failed to fetch') }, 'approve', 'live_order');
    expect(o.title).toBe('Not confirmed.');
    expect(o.unanswered).toBe(true);
    expect(text(o)).toContain("We don't know whether the order was placed");
    expect(text(o)).not.toContain('the code was accepted');
  });

  it('non-live actions keep the generic executor-failure copy even with the action passed (R24)', () => {
    const o = approvalOutcome(
      { err: envelope(503, 'approval_execution_failed', "The approved action 'kill_switch_release' did not run: boom", 'unavailable') },
      'approve',
      'kill_switch_release',
    );
    expect(o.title).toBe("Approved, but it didn't run.");
    expect(o.lines).toEqual(["The approved action 'kill_switch_release' did not run: boom", 'Ask for a new one.']);
  });

  it('a decision that happened elsewhere and an engine outage', () => {
    const decided = approvalOutcome({ err: envelope(409, 'already_decided', 'This approval was already approved', 'invalid') });
    expect(decided.tone).toBe('caution');
    expect(decided.title).toBe('This approval was already approved');
    const outage = approvalOutcome({ err: envelope(503, 'engine_not_ready', 'x', 'unavailable') });
    expect(outage.title).toBe('Not confirmed.');
    expect(text(outage)).toContain("We couldn't reach the engine, so we don't know whether the code was accepted");
  });

  it('a 409 executor_not_registered names the state and carries the server message, never an outage (A1)', () => {
    const o = approvalOutcome({
      err: envelope(
        409,
        'executor_not_registered',
        "No executor is registered for 'kill_switch_release'; the approval stays pending and the code was not used",
        'invalid',
        { action: 'kill_switch_release' },
      ),
    });
    expect(o.tone).toBe('caution');
    expect(o.title).toBe('The engine cannot execute this action yet.');
    expect(o.lines).toEqual([
      "No executor is registered for 'kill_switch_release'; the approval stays pending and the code was not used",
      'The code was not used. Wait for the engine, or deny this request.',
    ]);
    expect(text(o)).not.toContain("couldn't reach the engine");
  });

  it('a network-failed deny says the denial is unknown and never speaks of a code (A5)', () => {
    const o = approvalOutcome({ err: new TypeError('Failed to fetch') }, 'deny');
    expect(o.title).toBe('Not confirmed.');
    expect(text(o)).toContain("We couldn't reach the engine, so we don't know whether the denial was recorded");
    expect(text(o)).not.toContain('the code');
  });
});

describe('liveGateOutcome', () => {
  const armed: ApproveResponse = { executed: true, result: { armed: true }, actor: 'user:soma', approved_by: 'user:soma' };

  it('a decided arm says live trading is on, and what still needs a code', () => {
    const o = liveGateOutcome({ ok: armed });
    expect(o.tone).toBe('none');
    expect(text(o)).toBe('Live trading is on. Each live session start and manual live order still needs its own one-time code.');
  });

  it("a wrong code keeps it off with the frozen sentence inline (08 §6)", () => {
    const o = liveGateOutcome({ err: envelope(422, 'code_mismatch', "That code didn't match. Nothing changed.", 'invalid', { attempts_left: 4 }) });
    expect(o.inline).toBe(true);
    expect(o.title).toBe("That code didn't match. Nothing changed.");
    expect(o.lines).toEqual(['4 attempts left.']);
  });

  it('a challenge the code service could not create armed nothing, and live actions hide (fail-safe)', () => {
    const o = liveGateOutcome({ err: envelope(503, 'engine_unreachable', 'trader unreachable', 'unavailable') }, 'challenge');
    expect(o.tone).toBe('caution');
    expect(o.title).toBe("Live trading wasn't turned on.");
    expect(o.lines).toEqual(['Live actions are hidden until the code service recovers.']);
    expect(o.actions).toEqual([{ kind: 'retry', label: 'Try again' }]);
  });

  it('an arm the executor could not run stays off, naming why', () => {
    const msg = "The approved action 'live_arming' did not run: credentials not live-capable";
    const o = liveGateOutcome({ err: envelope(503, 'approval_execution_failed', msg, 'unavailable') });
    expect(o.title).toBe("Live trading wasn't turned on.");
    expect(o.lines).toEqual([msg]);
  });

  it('an approve the engine never answered is not confirmed, never claimed off', () => {
    const o = liveGateOutcome({ err: new TypeError('Failed to fetch') });
    expect(o.title).toBe('Not confirmed.');
    expect(text(o)).toContain("we don't know whether live trading was turned on");
  });

  it('a 503 engine_unreachable on the approve leg is in-flight-unknown, never claimed off (R30(1))', () => {
    const msg =
      'The trading engine did not answer within 20s — the operation may still be executing; check the session or order state before re-submitting';
    const o = liveGateOutcome({ err: envelope(503, 'engine_unreachable', msg, 'unavailable') });
    expect(o.title).toBe('Not confirmed.');
    expect(o.unanswered).toBe(true);
    expect(text(o)).toContain("we don't know whether live trading was turned on");
    expect(o.lines).toContain(msg);
  });

  it("a definite executor refusal (R8's credentials_not_live_capable) says why, never 'couldn't reach'", () => {
    const msg = 'The stored Binance keys can withdraw; connect trade-only keys before arming';
    const o = liveGateOutcome({ err: envelope(409, 'credentials_not_live_capable', msg, 'invalid') });
    expect(o.title).toBe("Live trading wasn't turned on.");
    expect(o.lines).toEqual([msg]);
    expect(text(o)).not.toContain("couldn't reach the engine");
  });
});

describe('liveOrderOutcome', () => {
  const SID = session().session_id;
  const req: QuickTradeRequest = { symbol: 'BTCUSDT', side: 'buy', order_type: 'MARKET', quantity: '0.0015', session_id: SID };
  const approve = (result: unknown): ApproveResponse => ({ executed: true, result, actor: 'service:mcp', approved_by: 'user:soma' });
  /** The live executor returns the paper response shapes (seam #4), the orderListId as the id. */
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
    protective_order_id: 'oco-5',
    exit_orders_deferred: false,
    fill_price: 67612.4,
    mark_source: 'live',
    ...over,
  });

  it('a filled buy says real money, the fill, the exchange stop and the order id', () => {
    const o = liveOrderOutcome({ ...req, stop_loss: '64256' }, { ok: approve(res()) }, SID);
    expect(o.title).toBe('Bought 0.00150 BTC with real money.');
    expect(o.lines).toEqual([
      'Filled at 67,612.40.',
      'Stop requested at 64,256.00.',
      'The position shows the stop actually placed on the exchange.',
      'Order a1b2c3d4.',
    ]);
  });

  it('a default-stop buy, and a sell that only reduces', () => {
    const buy = liveOrderOutcome(req, { ok: approve(res()) }, SID);
    expect(buy.lines).toEqual(['Filled at 67,612.40.', 'Stop placed on the exchange.', 'Order a1b2c3d4.']);
    const sell = liveOrderOutcome({ ...req, side: 'sell' }, { ok: approve(res({ side: 'SELL', protection: 'none', fill_price: 67590.1 })) }, SID);
    expect(sell.title).toBe('Sold 0.00150 BTC with real money.');
    expect(sell.lines).toEqual(['Filled at 67,590.10.', 'This reduces the held position; no stop is needed.', 'Order a1b2c3d4.']);
  });

  it('a placed limit says real money and keeps the deferred-stop line', () => {
    const o = liveOrderOutcome(req, { ok: approve(res({ status: 'SUBMITTED', order_type: 'LIMIT', protection: 'pending', exit_orders_deferred: true })) }, SID);
    expect(text(o)).toBe('Limit order placed with real money. The stop will be placed when it fills. Order a1b2c3d4.');
  });

  it('a filled buy with no exchange stop is danger, the session one click away (the dead-man)', () => {
    const o = liveOrderOutcome(req, { ok: approve(res({ protection: 'pending', protective_order_id: null, exit_orders_deferred: true })) }, SID);
    expect(o.tone).toBe('danger');
    expect(o.title).toBe('Bought 0.00150 BTC with real money, but no exchange stop could be placed.');
    expect(o.actions).toEqual([{ kind: 'open', label: 'Open session', href: `/trading/${SID}` }]);
  });

  it('a wrong code is the frozen sentence inline; a refused risk re-check reuses the quick-trade mapping', () => {
    const mismatch = liveOrderOutcome(req, { err: envelope(422, 'code_mismatch', "That code didn't match. Nothing was placed.", 'invalid', { attempts_left: 2 }) }, SID);
    expect(mismatch.inline).toBe(true);
    expect(mismatch.title).toBe("That code didn't match. Nothing was placed.");
    expect(mismatch.lines).toEqual(['2 attempts left.']);
    const refused = liveOrderOutcome(req, { err: envelope(422, 'risk_rejected', 'Daily loss limit reached', 'invalid', { check: 'daily_loss', intent: 'entry' }) }, SID);
    expect(refused.title).toBe('Not placed: Daily loss limit reached (daily_loss).');
  });

  it('a challenge the code service could not create placed nothing, and live actions hide (fail-safe)', () => {
    const o = liveOrderOutcome(req, { err: envelope(503, 'engine_unreachable', 'trader unreachable', 'unavailable') }, SID, 'challenge');
    expect(o.title).toBe('No order was placed.');
    expect(o.lines).toEqual(['Live actions are hidden until the code service recovers.']);
  });

  it('an executor failure is not-confirmed — a real order may exist on Binance', () => {
    const msg = "The approved action 'live_order' did not run: exchange unreachable";
    const o = liveOrderOutcome(req, { err: envelope(503, 'approval_execution_failed', msg, 'unavailable') }, SID);
    expect(o.tone).toBe('danger');
    expect(o.title).toBe('Not confirmed.');
    expect(o.unanswered).toBe(true);
    expect(o.lines).toContain(msg);
    expect(o.actions).toEqual([{ kind: 'open', label: 'Open session', href: `/trading/${SID}` }]);
  });

  it('an unknown submit names the client order id it may have landed under', () => {
    const o = liveOrderOutcome(req, { err: envelope(503, 'order_submit_unknown', 'submit unknown', 'unavailable', { client_order_id: 'hydra-abc123-x' }) }, SID);
    expect(o.tone).toBe('danger');
    expect(o.unanswered).toBe(true);
    expect(o.lines).toContain('Binance may hold order hydra-ab.');
  });

  it('an approve the engine never answered is the quick-trade not-confirmed mapping, plus the session', () => {
    const o = liveOrderOutcome(req, { err: new TypeError('Failed to fetch') }, SID);
    expect(o.title).toBe('Not confirmed.');
    expect(text(o)).toContain("we don't know whether the live order was placed");
    expect(text(o)).toContain("Check this session's fills before trying again.");
    expect(o.actions).toEqual([{ kind: 'open', label: 'Open session', href: `/trading/${SID}` }]);
  });
});

describe('liveStartOutcome', () => {
  const startReq: LiveSessionStartRequest = { strategy_id: 'sma-cross-btc-1h', paper_capital: '8000', reason: 'Live session: SMA cross' };
  const startReqBody = { ...session({ started_at: '2026-10-21T14:03:00+00:00' }), paper_capital: 6000, trading_mode: 'live' };

  it('a started live session says real money and navigates to it, clamped like paper (case 7)', () => {
    const o = liveStartOutcome(
      startReq,
      { ok: { executed: true, result: startReqBody, actor: 'user:soma', approved_by: 'user:soma' } },
      'approve',
      NOW,
    );
    expect(o.tone).toBe('none');
    expect(o.title).toBe('Started 14:03:00 UTC.');
    expect(o.lines).toEqual([
      'Warming up. Waiting for the first closed 1h bar.',
      'This session places real orders on Binance with real money.',
      'Asked 8,000.00; started with 6,000.00 (all that was unallocated).',
    ]);
    expect(o.navigate).toBe(`/trading/${startReqBody.session_id}`);
  });

  it('a refused executor run says nothing was started, never claims a session', () => {
    const o = liveStartOutcome(startReq, { ok: { executed: false, result: null, actor: 'user:soma', approved_by: 'user:soma' } });
    expect(o.title).toBe('Nothing was started.');
    expect(o.navigate).toBeUndefined();
  });

  it("a wrong code keeps the frozen sentence inline with the start's tail", () => {
    const o = liveStartOutcome(startReq, { err: envelope(422, 'code_mismatch', "That code didn't match. Nothing changed.", 'invalid', { attempts_left: 3 }) });
    expect(o.inline).toBe(true);
    expect(o.title).toBe("That code didn't match. Nothing was started.");
    expect(o.lines).toEqual(['3 attempts left.']);
  });

  it('a challenge the code service could not create started nothing', () => {
    const o = liveStartOutcome(startReq, { err: envelope(503, 'engine_unreachable', 'trader unreachable', 'unavailable') }, 'challenge');
    expect(o.title).toBe('Nothing was started.');
    expect(o.lines).toEqual(['Live actions are hidden until the code service recovers.']);
    expect(o.actions).toEqual([{ kind: 'retry', label: 'Try again' }]);
  });

  it('a definite executor refusal uses the start vocabulary (kill_switch_active)', () => {
    const o = liveStartOutcome(startReq, { err: envelope(409, 'kill_switch_active', 'Kill switch is on', 'invalid') });
    expect(o.title).toBe('Nothing was started.');
    expect(o.lines).toEqual(['The kill switch is on. Release it on Risk before starting a session.']);
  });

  it('a 503 engine_unreachable on the approve leg is in-flight-unknown, never "Nothing was started." (R30(1))', () => {
    const msg =
      'The trading engine did not answer within 20s — the operation may still be executing; check the session or order state before re-submitting';
    const o = liveStartOutcome(startReq, { err: envelope(503, 'engine_unreachable', msg, 'unavailable') });
    expect(o.title).toBe('Not confirmed.');
    expect(o.unanswered).toBe(true);
    expect(text(o)).toContain(
      "We couldn't reach the engine, so we don't know whether the live session started. Check the sessions list before trying again.",
    );
    expect(o.lines).toContain(msg);
    expect(o.actions).toEqual([{ kind: 'open', label: 'Open Sessions', href: '/trading' }]);
  });

  it('a 504 or a raw network error stays not-confirmed on the approve leg', () => {
    const gateway = liveStartOutcome(startReq, { err: envelope(504, 'http_504', 'gateway timeout', 'unavailable') });
    expect(gateway.title).toBe('Not confirmed.');
    expect(gateway.unanswered).toBe(true);
    const net = liveStartOutcome(startReq, { err: new TypeError('Failed to fetch') });
    expect(net.title).toBe('Not confirmed.');
    expect(net.unanswered).toBe(true);
  });

  it('an executed approve without a session answer stays unknown — the session may exist unwatched', () => {
    const o = liveStartOutcome(startReq, { ok: { executed: true, result: null, actor: 'user:soma', approved_by: 'user:soma' } });
    expect(o.tone).toBe('danger');
    expect(o.unanswered).toBe(true);
    expect(text(o)).toBe("Not confirmed. We don't know whether the session started. Check the sessions list before trying again.");
    expect(o.actions).toEqual([{ kind: 'open', label: 'Open Sessions', href: '/trading' }]);
  });
});
