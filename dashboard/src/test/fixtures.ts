/** Response bodies as the engine serialises them at main; each builder cites its model. */

import type { CircuitBreaker, LiveStatus, PlatformConfig, SessionDetail, SessionResponse, TradeItem } from '@/lib/derive';

/** trading.py:52-67 SessionResponse, filled by `_session_to_dict` (trading.py:229-245) — no A3+ fields. */
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

/** trading.py:110-131 SessionDetailResponse, built by `get_session_detail` (session_manager.py:388-453). */
export function detail(over: Partial<SessionDetail> = {}): SessionDetail {
  return {
    ...session(),
    last_bar_at: '2026-10-21T14:00:04+00:00',
    entries_paused: null,
    protection_failures: [],
    metrics: { balance: { USDT: 4898.58 }, total_pnl: 0, win_rate: 0.0, total_trades: 1, open_positions: 1 },
    positions: [],
    trades: [],
    ...over,
  };
}

/** A paper fill: `_fill_response` (paper_trading.py:1360-1377) sends `amount`, so TradeItem.quantity defaults to 0 (trading.py:82). */
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
