/** Response bodies as the engine serialises them at main; each builder cites its model. */

import type {
  ApprovalDetail,
  CircuitBreaker,
  ExchangeStopState,
  LiveStatus,
  OpenOrder,
  PendingApproval,
  PerpsSessionState,
  PlatformConfig,
  PositionItem,
  ReconcileState,
  SessionDetail,
  SessionResponse,
  TradeItem,
} from '@/lib/derive';
import type { DisarmResponse, LiveApprovalChallenge, PerpsDisarmResponse, PerpsStatusResponse } from '@/lib/outcomes';

/** trading.py:56-75 SessionResponse, filled by `_session_to_dict` (trading.py:250-265) — no A3+ fields. */
export function session(over: Partial<SessionResponse> = {}): SessionResponse {
  return {
    session_id: 'a1b2c3d4-0000-4000-8000-000000000001',
    strategy_id: 'sma-cross-btc-1h',
    strategy_name: 'SMA cross',
    trading_mode: 'paper',
    status: 'running',
    exchange_id: 'binance',
    symbols: ['BTCUSDT'],
    timeframe: '1h',
    paper_capital: 5000,
    started_at: '2026-10-21T08:00:00+00:00',
    stopped_at: null,
    error_message: null,
    positions_at_stop: 0,
    flatten_incomplete: false,
    persist_error: null,
    ...over,
  };
}

/** A P3 live session: `trading_mode: 'live'` reaches the API only once P3-A/C land (403 before). */
export function liveSession(over: Partial<SessionResponse> = {}): SessionResponse {
  return session({ trading_mode: 'live', ...over });
}

/** trading.py:130-150 SessionDetailResponse, built by `get_session_detail` (session_manager.py:388-453). */
export function detail(over: Partial<SessionDetail> = {}): SessionDetail {
  return {
    ...session(),
    last_bar_at: '2026-10-21T14:00:04+00:00',
    entries_paused: null,
    protection_failures: [],
    metrics: { equity: 4998.58, realized_pnl: 0, unrealized_pnl: -1.42, fees: 0.1, drawdown: 0.0003 },
    positions: [],
    trades: [],
    open_orders: [],
    // A7 sends null on paper; live details carry the block below.
    reconcile: null,
    ...over,
  };
}

/** A7 (pinned seam #2): the reconcile block a live detail carries; null on paper sessions. */
export function reconcile(over: Partial<ReconcileState> = {}): ReconcileState {
  return { state: 'ok', last_reconciled_at: '2026-10-21T14:35:04+00:00', detail: null, ...over };
}

/** A8-live (pinned seam #3, B's ExchangeStopState) as it rides a held position on a live detail. */
export function exchangeStop(over: Partial<ExchangeStopState> = {}): ExchangeStopState {
  return {
    state: 'confirmed',
    kind: 'oco',
    stop_price: 64256,
    take_profit: 69800,
    order_list_id: '5',
    checked_at: '2026-10-21T14:35:04+00:00',
    ...over,
  };
}

/** A pre-A9 paper fill: `_fill_response` sent only `amount`, so TradeItem.quantity defaulted to 0 (trading.py:90); since A9 it also sends `quantity` (paper_trading.py:1360-1379). */
export function paperFill(over: Partial<TradeItem> = {}): TradeItem {
  return {
    id: 'ord-1',
    symbol: 'BTC/USDT',
    side: 'BUY',
    quantity: 0,
    amount: 0.0015,
    price: 67612.4,
    fee: 0.1,
    pnl: 0,
    net_pnl: null,
    timestamp: '2026-10-21T14:00:04+00:00',
    ...over,
  };
}

/** A P4 perp session (paper or live): the venue every perp surface keys on. */
export function perpSession(over: Partial<SessionResponse> = {}): SessionResponse {
  return session({ exchange_id: 'binanceusdm', symbols: ['BTCUSDT'], ...over });
}

/** P4-C's `perps` block on a FUTURES session detail (session_manager.get_session_detail):
 * the margin wallet, leverage, funding accrual counters and the liquidation count. */
export function perpsState(over: Partial<PerpsSessionState> = {}): PerpsSessionState {
  return {
    leverage: 2,
    margin_mode: 'isolated',
    funding_applied: 3,
    funding_missing: 0,
    liquidations: 0,
    wallet_available: 400.5,
    margin_used: 99.5,
    ...over,
  };
}

/** P7-G: a paper perp session's resting NO-OCO protective pair as `fetch_open_orders`
 * serves it (perp_trading.py `_create_oco` through services/sessions.py
 * `json_safe_open_order`): both legs share `oco_id`, each carries its trigger in
 * `stop_price` (the take-profit leg is TAKE_PROFIT_MARKET, not a LIMIT), reduce-only. */
export function perpPair(over: Partial<OpenOrder> = {}): OpenOrder[] {
  const shared = {
    symbol: 'BTCUSDT',
    side: 'SELL',
    amount: 0.002,
    price: null,
    protective: true,
    oco_id: 'p7g-pair-1',
    trail_pct: null,
    reduce_only: true,
    ...over,
  };
  return [
    { ...shared, id: 'p7g-tp', type: 'TAKE_PROFIT_MARKET', stop_price: 71000, reason: 'take_profit' },
    { ...shared, id: 'p7g-sl', type: 'STOP_MARKET', stop_price: 64000, reason: 'stop_loss' },
  ];
}

/** P7-G: a perp trailing protective leg — `trail_pct` is the fraction in (0, 1)
 * (`ProtectionSpec`'s own doc), `stop_price` the trigger after the peak moved. */
export function perpTrailingStop(over: Partial<OpenOrder> = {}): OpenOrder {
  return {
    id: 'p7g-trail',
    symbol: 'BTCUSDT',
    side: 'SELL',
    type: 'TRAILING_STOP',
    amount: 0.002,
    price: null,
    stop_price: 64800,
    reason: 'trailing_stop',
    protective: true,
    oco_id: null,
    trail_pct: 0.02,
    reduce_only: true,
    ...over,
  };
}

/** P7-G: a perp session's hedge-capable positions — one-way position mode holds one
 * signed position per symbol, so both sides live on different symbols. */
export function perpHedgedPositions(): PositionItem[] {
  return [
    { symbol: 'BTCUSDT', direction: 'long', quantity: 0.002, avg_entry_price: 60000, unrealized_pnl: 10 },
    { symbol: 'ETHUSDT', direction: 'short', quantity: 0.0015, avg_entry_price: 3400, unrealized_pnl: -2.5 },
  ];
}

/** risk.py:130-135 RiskStatusLive as returned by `get_live_risk_status` (risk.py:532-567). */
export function liveStatus(over: Partial<LiveStatus> = {}): LiveStatus {
  return {
    kill_switch_active: false,
    kill_switch_state: 'inactive',
    running_sessions: 1,
    circuit_breaker_restrictions: {
      active_tier: 0,
      can_open_new_trades: true,
      size_multiplier: 1,
      must_flatten: false,
      is_shutdown: false,
      daily_halt_until: null,
      consecutive_pause_until: null,
    },
    circuit_breakers_state: 'live',
    // P3-C adds it (must-fix 6, R9): fail-safe false when the armed row can't be read.
    live_armed: false,
    ...over,
  };
}

/** risk.py:36-44 CircuitBreaker from `_breakers` (risk.py:217-231); percents are ×100. */
export function breaker(over: Partial<CircuitBreaker> = {}): CircuitBreaker {
  return {
    tier: 1,
    label: 'Reduce size 50%',
    threshold: '5.00% drawdown',
    threshold_pct: 5,
    current_value: 1.84,
    status: 'Normal',
    active_since: null,
    requires_manual_review: false,
    ...over,
  };
}

/** system.py:298-309 PlatformConfig (only the fields the UI reads). */
export function platformConfig(over: Partial<PlatformConfig> = {}): PlatformConfig {
  return { trading_mode: 'paper', paper_capital: 10000, paper_capital_pending: null, ...over };
}

/** routes/activity.py PendingApproval (activity.py:80-88); the actor is the requester (C's requested_by):
 * 'service:mcp' is Claude, a user principal is your own challenge. */
export function pendingApproval(over: Partial<PendingApproval> = {}): PendingApproval {
  return {
    id: '3f0b9e2a-1000-4000-8000-000000000001',
    actor: 'service:mcp',
    action: 'kill_switch_release',
    reason: 'drawdown recovered, resume trading',
    params: { reason: 'drawdown recovered, resume trading' },
    requested_at: '2026-10-21T13:50:04+00:00',
    expires_at: '2026-10-21T14:00:04+00:00',
    status: 'pending',
    ...over,
  };
}

/** routes/activity.py ApprovalDetail (activity.py:91-95). */
export function approvalDetail(over: Partial<ApprovalDetail> = {}): ApprovalDetail {
  return { ...pendingApproval(), attempts_left: 5, approved_by: null, decided_at: null, error: null, ...over };
}

/** R9: the single create-route answer for /api/live/arm, /session-start, /order and /credential-delete —
 * the engine-issued challenge the L4 dialog shows once; the code is typed into the approve endpoint. */
export function liveChallenge(over: Partial<LiveApprovalChallenge> = {}): LiveApprovalChallenge {
  return {
    status: 'approval_required',
    approval_id: '3f0b9e2a-2000-4000-8000-000000000001',
    code: '482913',
    expires_at: '2026-10-21T14:10:04+00:00',
    params: { live_capital: 5000 },
    note: 'Enter this code once to turn on live trading.',
    ...over,
  };
}

/** R10: POST /api/live/disarm names the live sessions that keep running (ids + count). */
export function disarmResponse(over: Partial<DisarmResponse> = {}): DisarmResponse {
  return {
    live_armed: false,
    running_live_sessions: [],
    running_live_sessions_count: 0,
    ...over,
  };
}

/** GET /api/live/perps/status (P4-E): the separate perps ceremony's rows, beside the spot gate's. */
export function perpsStatus(over: Partial<PerpsStatusResponse> = {}): PerpsStatusResponse {
  return {
    perps_armed: false,
    armed_by: null,
    armed_at: null,
    perps_capital: null,
    running_live_perps_sessions: [],
    running_live_perps_sessions_count: 0,
    ...over,
  };
}

/** POST /api/live/perps/disarm (the perps R10): running live perp sessions keep running. */
export function perpsDisarmResponse(over: Partial<PerpsDisarmResponse> = {}): PerpsDisarmResponse {
  return {
    ...perpsStatus(),
    status: 'disarmed',
    message: 'Perps trading is disarmed.',
    pending_perps_approvals: [],
    ...over,
  };
}
