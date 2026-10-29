import { describe, expect, it } from 'vitest';
import {
  actionPhrase,
  askPhrase,
  breakerRow,
  dataAnswer,
  exchangeStopRow,
  fillQty,
  heldAfter,
  isOverdue,
  ksTargets,
  modeBadge,
  needsLiveOrderReview,
  needsModeLabels,
  needsYou,
  reconcileRow,
  rowMode,
  sideLabel,
  stoppedHolders,
  stopRuleLabel,
  timeframeSeconds,
  winRate,
  type CircuitBreaker,
  type PendingApproval,
  type PositionItem,
  type ReconcileState,
  type SessionDetail,
} from './derive';
import { breaker, detail, exchangeStop, liveSession, liveStatus, paperFill, pendingApproval, platformConfig, reconcile, session } from '@/test/fixtures';

const NOW = Date.parse('2026-10-21T14:40:00Z');
const ago = (ms: number) => new Date(NOW - ms).toISOString();
const MIN = 60_000;
const HOUR = 60 * MIN;

describe('timeframeSeconds / isOverdue', () => {
  it('reads every core.types Timeframe (core/types.py:42-51) and rejects others', () => {
    expect(['1m', '5m', '15m', '1h', '4h', '1d', '1w'].map(timeframeSeconds)).toEqual([
      60, 300, 900, 3600, 14400, 86400, 604800,
    ]);
    expect(timeframeSeconds('1M')).toBeNull();
  });

  it('matches HydraClosedBarOverdue: age > 2 × tf + 60 s', () => {
    expect(isOverdue(ago(2 * HOUR + 59_000), '1h', NOW)).toBe(false);
    expect(isOverdue(ago(2 * HOUR + 61_000), '1h', NOW)).toBe(true);
    expect(isOverdue(ago(31 * MIN + 1_000), '15m', NOW)).toBe(true);
    expect(isOverdue(null, '1h', NOW)).toBeNull();
  });
});

describe('dataAnswer', () => {
  const s15 = session({ session_id: 's15', strategy_name: 'RSI mean-revert', timeframe: '15m' });
  const s1h = session({ session_id: 's1h' });

  it('names the late 15m session while the 1h one is fine (case 5)', () => {
    const details = new Map<string, SessionDetail | Error>([
      ['s15', detail({ session_id: 's15', timeframe: '15m', last_bar_at: ago(40 * MIN), entries_paused: 'feed stale: BTCUSDT 15m' })],
      ['s1h', detail({ session_id: 's1h', last_bar_at: ago(40 * MIN) })],
    ]);
    expect(dataAnswer([s15, s1h], details, NOW)).toEqual({
      kind: 'late',
      late: 1,
      total: 2,
      worst: { name: 'RSI mean-revert', tf: '15m', overdueMs: 25 * MIN, paused: 'feed stale: BTCUSDT 15m' },
    });
  });

  it('never counts an unreadable detail as on time', () => {
    const details = new Map<string, SessionDetail | Error>([
      ['s15', new Error('503')],
      ['s1h', detail({ session_id: 's1h', last_bar_at: ago(10 * MIN) })],
    ]);
    expect(dataAnswer([s15, s1h], details, NOW)).toEqual({ kind: 'ok', total: 2, unreadable: 1, paused: [] });
  });

  it('uses A3+ list fields when the detail is missing, and only when the list carries them', () => {
    const withA3 = session({ session_id: 's15', timeframe: '15m', last_bar_at: ago(5 * MIN), entries_paused: null });
    expect(dataAnswer([withA3], new Map(), NOW)).toEqual({ kind: 'ok', total: 1, unreadable: 0, paused: [] });
  });

  it('keeps the unreadable count and paused reasons when a session is warming up', () => {
    const s5 = session({ session_id: 's5', strategy_name: 'Breakout', timeframe: '5m' });
    const details = new Map<string, SessionDetail | Error>([
      ['s1h', detail({ session_id: 's1h', last_bar_at: null })],
      ['s15', new Error('503')],
      ['s5', detail({ session_id: 's5', timeframe: '5m', last_bar_at: ago(2 * MIN), entries_paused: 'feed stale: BTCUSDT 5m' })],
    ]);
    expect(dataAnswer([s1h, s15, s5], details, NOW)).toEqual({
      kind: 'warming',
      name: 'SMA cross',
      tf: '1h',
      total: 3,
      unreadable: 1,
      paused: ['feed stale: BTCUSDT 5m'],
    });
  });

  it('is unknown, never ok, when no running session could be read', () => {
    const details = new Map<string, SessionDetail | Error>([['s1h', new Error('503')]]);
    expect(dataAnswer([s1h], details, NOW)).toEqual({ kind: 'unknown', total: 1 });
    expect(dataAnswer([s15], new Map(), NOW)).toEqual({ kind: 'unknown', total: 1 });
  });

  it('reports warming up, paused-but-on-time and nothing running', () => {
    const warm = new Map<string, SessionDetail | Error>([['s1h', detail({ session_id: 's1h', last_bar_at: null })]]);
    expect(dataAnswer([s1h], warm, NOW)).toEqual({ kind: 'warming', name: 'SMA cross', tf: '1h', total: 1, unreadable: 0, paused: [] });
    const paused = new Map<string, SessionDetail | Error>([
      ['s1h', detail({ session_id: 's1h', last_bar_at: ago(10 * MIN), entries_paused: 'feed stale: BTCUSDT 1h' })],
    ]);
    expect(dataAnswer([s1h], paused, NOW)).toEqual({ kind: 'ok', total: 1, unreadable: 0, paused: ['feed stale: BTCUSDT 1h'] });
    expect(dataAnswer([session({ status: 'stopped' })], new Map(), NOW)).toEqual({ kind: 'none' });
  });
});

describe('holders and targets', () => {
  const running = session({ session_id: 'r', status: 'running', flatten_incomplete: true });
  const stoppedHolder = session({ session_id: 'h', status: 'stopped', flatten_incomplete: true, positions_at_stop: 1 });
  const erroredHolder = session({ session_id: 'e', status: 'error', positions_at_stop: 2 });
  const clean = session({ session_id: 'c', status: 'stopped' });

  it('stoppedHolders excludes running and errored sessions', () => {
    expect(stoppedHolders([running, stoppedHolder, erroredHolder, clean]).map((s) => s.session_id)).toEqual(['h']);
  });

  it('heldAfter is status-agnostic apart from running (session_manager.py:644)', () => {
    expect(heldAfter([running, stoppedHolder, erroredHolder, clean]).map((s) => s.session_id)).toEqual(['h', 'e']);
  });

  it('ksTargets mirrors _stop_targets: running certain, errored listed apart (session_manager.py:302-308)', () => {
    const t = ksTargets([running, stoppedHolder, erroredHolder, clean]);
    expect(t.running.map((s) => s.session_id)).toEqual(['r']);
    expect(t.errored.map((s) => s.session_id)).toEqual(['e']);
  });
});

describe('live mode (seam #10)', () => {
  it('LIVE when armed, even with no live session running', () => {
    expect(modeBadge(liveStatus({ live_armed: true }), [session()])).toBe('live');
  });

  it('LIVE when a live session runs, even with the armed read missing', () => {
    expect(modeBadge(liveStatus(), [session(), liveSession()])).toBe('live');
    expect(modeBadge(null, [liveSession()])).toBe('live');
  });

  it('PAPER when both sources read and stay quiet — a stopped live row only once it holds nothing', () => {
    expect(modeBadge(liveStatus(), [session()])).toBe('paper');
    expect(modeBadge(liveStatus(), [liveSession({ status: 'stopped' })])).toBe('paper');
  });

  it('LIVE for a stopped live session still holding positions — never PAPER over real money (R13/R23)', () => {
    expect(modeBadge(liveStatus(), [liveSession({ status: 'stopped', positions_at_stop: 2 })])).toBe('live');
    expect(modeBadge(null, [liveSession({ status: 'stopped', positions_at_stop: 1 })])).toBe('live');
  });

  it('an unreadable source is null — the caller renders "?", never PAPER (R23)', () => {
    expect(modeBadge(null, null)).toBeNull();
    expect(modeBadge(liveStatus(), null)).toBeNull();
    expect(modeBadge(null, [session()])).toBeNull();
  });

  it('row labels only when a view contains a live row; a stopped live row still counts (08 §6)', () => {
    expect(needsModeLabels([session(), liveSession({ status: 'stopped' })])).toBe(true);
    expect(needsModeLabels([session({ status: 'stopped' }), session()])).toBe(false);
    expect(rowMode(liveSession())).toBe('live');
    expect(rowMode(session())).toBe('paper');
  });

  it('only a running live session needs the L4 review', () => {
    expect(needsLiveOrderReview(liveSession())).toBe(true);
    expect(needsLiveOrderReview(liveSession({ status: 'stopped' }))).toBe(false);
    expect(needsLiveOrderReview(session())).toBe(false);
  });
});

const position = (exchange_stop: PositionItem['exchange_stop']): PositionItem => ({
  symbol: 'BTCUSDT',
  direction: 'long',
  quantity: 0.0015,
  avg_entry_price: 67612.4,
  unrealized_pnl: -1.42,
  exchange_stop,
});

describe('reconcile and exchange stops (A7/A8-live seams)', () => {
  it('ok reads as the age of the last pass; never says not reconciled yet', () => {
    expect(reconcileRow(reconcile({ last_reconciled_at: ago(2 * MIN) }), NOW)).toEqual({ text: 'Reconciled 2 min ago', tone: 'none' });
    expect(reconcileRow(reconcile({ state: 'never', last_reconciled_at: null }), NOW)).toEqual({ text: 'Not reconciled yet', tone: 'none' });
  });

  it('mismatch carries the drift verbatim as caution', () => {
    expect(reconcileRow(reconcile({ state: 'mismatch', detail: 'balance drift USDT local 100.00 exchange 98.00' }), NOW)).toEqual({
      text: 'Mismatch: balance drift USDT local 100.00 exchange 98.00',
      tone: 'caution',
    });
  });

  it('an unreadable or absent reconcile renders nothing here — the caller shows UNKNOWN', () => {
    expect(reconcileRow(reconcile({ state: 'unknown' }), NOW)).toBeNull();
    expect(reconcileRow(null, NOW)).toBeNull();
  });

  it('a state outside the vocabulary renders nothing here — the caller shows UNKNOWN (fail-closed)', () => {
    expect(reconcileRow(reconcile({ state: 'stale' as ReconcileState['state'] }), NOW)).toBeNull();
  });

  it('a confirmed stop names its price: "Stop at {price} on the exchange"', () => {
    expect(exchangeStopRow(position(exchangeStop()))).toEqual({ text: 'Stop at 64,256.00 on the exchange', tone: 'none' });
  });

  it('a missing or absent stop is danger - the dead-man invariant', () => {
    expect(exchangeStopRow(position(exchangeStop({ state: 'missing' })))).toEqual({ text: 'No exchange stop for BTCUSDT', tone: 'danger' });
    expect(exchangeStopRow(position(null))).toEqual({ text: 'No exchange stop for BTCUSDT', tone: 'danger' });
  });

  it('an unreadable verify renders nothing here — the caller shows UNKNOWN', () => {
    expect(exchangeStopRow(position(exchangeStop({ state: 'unknown' })))).toBeNull();
  });
});

describe('needsYou', () => {
  const base = { sessions: [session()], live: liveStatus(), config: platformConfig(), ksNotSaved: false, approvals: [] as PendingApproval[] };

  it('ignores protection failures: they live on session health (case 9)', () => {
    expect(needsYou(base)).toEqual({ items: [], count: 0, unavailable: [] });
  });

  it('counts a stopped holder, tier 3, an unsaved kill switch and pending capital, in order', () => {
    const r = needsYou({
      ...base,
      sessions: [session({ status: 'stopped', positions_at_stop: 1, flatten_incomplete: true, persist_error: 'db down' })],
      live: liveStatus({ circuit_breaker_restrictions: { ...liveStatus().circuit_breaker_restrictions!, active_tier: 3 } }),
      config: platformConfig({ paper_capital_pending: 12000 }),
      ksNotSaved: true,
    });
    expect(r.count).toBe(5);
    expect(r.items.map((i) => i.text)).toEqual([
      'SMA cross still holds 1 position after stopping',
      "SMA cross isn't being saved: db down",
      'Breaker tier 3 needs your review before trading resumes',
      'Kill switch is on but not saved. A restart would turn it off.',
      'Paper capital change to 12,000.00 USDT waits for a ledger reset',
    ]);
  });

  it('puts an errored session first', () => {
    const r = needsYou({ ...base, sessions: [session({ status: 'error', error_message: 'feed died' })] });
    expect(r.items).toEqual([
      { kind: 'error', text: 'SMA cross stopped with an error: feed died', href: '/trading/a1b2c3d4-0000-4000-8000-000000000001' },
    ]);
  });

  it('lists an errored session that still holds positions once, saying so', () => {
    const r = needsYou({ ...base, sessions: [session({ status: 'error', error_message: 'feed died', positions_at_stop: 2 })] });
    expect(r.count).toBe(1);
    expect(r.items.map((i) => i.text)).toEqual(['SMA cross stopped with an error: feed died; it still holds 2 positions']);
  });

  it('returns a null count when any source is unreadable', () => {
    expect(needsYou({ ...base, sessions: null }).count).toBeNull();
    expect(needsYou({ ...base, config: null })).toMatchObject({ count: null, unavailable: ['settings'] });
    const noBreakers = liveStatus({ circuit_breaker_restrictions: null, circuit_breakers_state: 'unavailable' });
    expect(needsYou({ ...base, live: noBreakers })).toMatchObject({ count: null, unavailable: ['breakers'] });
  });

  it('lists a pending approval above every other need, with its age (06 §2)', () => {
    const approval = pendingApproval({ requested_at: ago(2 * MIN) });
    const r = needsYou({
      ...base,
      approvals: [approval],
      sessions: [session({ status: 'error', error_message: 'feed died' })],
      now: NOW,
    });
    expect(r.count).toBe(2);
    expect(r.items.map((i) => i.kind)).toEqual(['approval', 'error']);
    expect(r.items[0]).toEqual({
      kind: 'approval',
      text: 'Claude asks to release the kill switch · 2 min ago',
      href: `/activity/approvals/${approval.id}`,
    });
  });

  it('never counts unknown approvals as none', () => {
    const r = needsYou({ ...base, approvals: null });
    expect(r.count).toBeNull();
    expect(r.unavailable).toContain('approvals');
    expect(r.items).toEqual([]);
  });

  it('phrases the gated action and falls back to a spaced action', () => {
    expect(actionPhrase('kill_switch_release')).toBe('release the kill switch');
    expect(actionPhrase('some_future_action')).toBe('some future action');
  });

  it('phrases the four P3 live executors after "Claude asks to" (seam #1)', () => {
    expect(actionPhrase('live_arming')).toBe('turn on live trading');
    expect(actionPhrase('live_session_start')).toBe('start a live session');
    expect(actionPhrase('live_order')).toBe('place a live order');
    expect(actionPhrase('credential_delete')).toBe('delete the stored exchange keys');
  });

  it('phrases the MCP-ML model registry actions (promote_model / rollback_model)', () => {
    expect(askPhrase('service:mcp', 'promote_model')).toBe('Claude asks to promote a model to Production');
    expect(askPhrase('service:mcp', 'rollback_model')).toBe(
      'Claude asks to roll a model back to its previous Production version',
    );
  });

  it('renders a user-initiated approval as your own action, never "Claude asks to" (must-fix 7)', () => {
    const mine = pendingApproval({ actor: 'user:soma', action: 'live_arming', requested_at: ago(2 * MIN) });
    const order = pendingApproval({ id: '3f0b9e2a-1000-4000-8000-000000000002', actor: 'user:soma', action: 'live_order', requested_at: ago(MIN) });
    const r = needsYou({ ...base, approvals: [mine, order], now: NOW });
    expect(r.items.map((i) => i.text)).toEqual([
      'You asked to turn on live trading · 2 min ago',
      'You asked to place a live order · 1 min ago',
    ]);
  });

  it('keeps "Claude asks to" for the MCP actor only', () => {
    const r = needsYou({ ...base, approvals: [pendingApproval({ requested_at: ago(3 * MIN) })], now: NOW });
    expect(r.items[0].text).toBe('Claude asks to release the kill switch · 3 min ago');
  });
});

describe('fills, sides and stop rules', () => {
  it('reads quantity from amount when quantity is the 0 default (case 6)', () => {
    expect(fillQty(paperFill())).toBe(0.0015);
    expect(fillQty(paperFill({ quantity: 0.002 }))).toBe(0.002);
    expect(fillQty({ quantity: 0 })).toBeNull();
  });

  it('labels portfolio, detail and fill sides (portfolio.py:427, session_manager.py:423, paper_trading.py:662)', () => {
    expect(sideLabel('Long')).toEqual({ text: 'Held', caution: false });
    expect(sideLabel('long')).toEqual({ text: 'Held', caution: false });
    expect(sideLabel('BUY')).toEqual({ text: 'Buy', caution: false });
    expect(sideLabel('sell')).toEqual({ text: 'Sell', caution: false });
    // P4: a perp SHORT is a first-class side, not an unknown vocabulary word.
    expect(sideLabel('Short')).toEqual({ text: 'Short', caution: false });
  });

  it('names the strategy stop rule from StrategyRiskConfig methods (strategy/config.py:45)', () => {
    expect(stopRuleLabel({ stop_loss_method: 'atr', stop_loss_value: 2 })).toBe('2.0 × ATR, set at fill');
    expect(stopRuleLabel({ stop_loss_method: 'fixed_pct', stop_loss_value: 5 })).toBe('5% below fill');
    expect(stopRuleLabel({ stop_loss_method: 'trailing_stop', stop_loss_value: 3 })).toBe('trailing 3%');
    expect(stopRuleLabel(new Error('404'))).toBe("rule couldn't be read");
  });
});

describe('breakerRow', () => {
  it('words each breaker state (risk/service.py:788-810)', () => {
    const since = '2026-10-21T08:00:00+00:00';
    expect(breakerRow(breaker({ tier: 1, status: 'Tripped', active_since: since }), 2, NOW)).toEqual({
      text: 'Tripped (included in tier 2)',
      tone: 'none',
    });
    expect(breakerRow(breaker({ tier: 2, status: 'Tripped', active_since: since }), 2, NOW)).toEqual({
      text: 'Active since 08:00:00 UTC',
      tone: 'caution',
    });
    expect(breakerRow(breaker({ tier: 3, threshold_pct: 15, current_value: 12.4, status: 'Warning' }), 2, NOW)).toEqual({
      text: 'Near: 12.40% of 15.00%',
      tone: 'caution',
    });
    expect(breakerRow(breaker({ tier: 4, threshold_pct: 20 }), 2, NOW)).toEqual({ text: '20.00%', tone: 'none' });
  });

  it('a status outside the vocabulary renders nothing here — the caller shows Unknown (fail-closed)', () => {
    expect(breakerRow(breaker({ status: 'stale' as CircuitBreaker['status'] }), 2, NOW)).toBeNull();
  });
});

describe('winRate', () => {
  it('is null with no closing fill, where the API says 0.0 (session_manager.py:446)', () => {
    expect(winRate([paperFill()])).toBeNull();
    expect(winRate([paperFill({ net_pnl: 12.5 }), paperFill({ net_pnl: -3 }), paperFill()])).toBe(50);
  });
});
