/**
 * The server pushes two events on `/ws/risk`:
 * - `risk_event`: one tick per new `risk_events` row — refetch (the trail and every
 *   derived number may have moved);
 * - `risk_state`: the diffed heartbeat — no refetch, the ledger numbers ride along.
 */

export interface RiskHeartbeat {
  event: 'risk_state';
  scope: string;
  equity: number | null;
  peak_equity: number | null;
  day_start_equity: number | null;
  daily_loss: number | null;
  current_drawdown: number;
  consecutive_losses: number;
  cb_tier: number;
  cb_tier_since: string | null;
  daily_halt_until: string | null;
  consecutive_pause_until: string | null;
  kill_switch_active: boolean | null;
  updated_at: string | null;
}

export type RiskWsMessage =
  | { action: 'refetch' }
  | { action: 'heartbeat'; beat: RiskHeartbeat }
  | { action: 'ignore' };

function heartbeatOrNull(m: Record<string, unknown>): RiskHeartbeat | null {
  const optional = (v: unknown): v is number | null => v === null || typeof v === 'number';
  const text = (v: unknown): string | null => (typeof v === 'string' ? v : null);
  if (
    !optional(m.equity) ||
    !optional(m.peak_equity) ||
    !optional(m.day_start_equity) ||
    !optional(m.daily_loss) ||
    typeof m.current_drawdown !== 'number' ||
    typeof m.consecutive_losses !== 'number' ||
    typeof m.cb_tier !== 'number'
  ) {
    return null;
  }
  return {
    event: 'risk_state',
    scope: typeof m.scope === 'string' ? m.scope : '',
    equity: m.equity,
    peak_equity: m.peak_equity,
    day_start_equity: m.day_start_equity,
    daily_loss: m.daily_loss,
    current_drawdown: m.current_drawdown,
    consecutive_losses: m.consecutive_losses,
    cb_tier: m.cb_tier,
    cb_tier_since: text(m.cb_tier_since),
    daily_halt_until: text(m.daily_halt_until),
    consecutive_pause_until: text(m.consecutive_pause_until),
    kill_switch_active: m.kill_switch_active === null ? null : Boolean(m.kill_switch_active),
    updated_at: text(m.updated_at),
  };
}

export function classifyRiskWsMessage(msg: unknown): RiskWsMessage {
  if (typeof msg !== 'object' || msg === null) return { action: 'ignore' };
  const m = msg as Record<string, unknown>;
  if (typeof m.event !== 'string') return { action: 'ignore' };
  if (m.event === 'risk_event') return { action: 'refetch' };
  if (m.event === 'risk_state') {
    const beat = heartbeatOrNull(m);
    return beat === null ? { action: 'ignore' } : { action: 'heartbeat', beat };
  }
  return { action: 'ignore' };
}
