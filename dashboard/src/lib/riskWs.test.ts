import { describe, expect, it } from 'vitest';
import { classifyRiskWsMessage } from './riskWs';

// The shape the RiskRelay broadcasts per new risk_events row.
const riskEvent = {
  event: 'risk_event',
  id: 42,
  scope: 'paper',
  kind: 'kill_switch_on',
  tier: null,
  actor: 'user:soma',
  reason: 'kill switch (api)',
};

// The shape the RiskRelay broadcasts when the risk_state row changes.
const riskState = {
  event: 'risk_state',
  scope: 'paper',
  equity: 9512.4,
  peak_equity: 10630.1,
  day_start_equity: 9632.4,
  daily_loss: 120.0,
  current_drawdown: 10.5,
  consecutive_losses: 2,
  cb_tier: 3,
  cb_tier_since: '2026-10-30T11:00:00+00:00',
  daily_halt_until: '2026-10-31T00:00:00+00:00',
  consecutive_pause_until: null,
  kill_switch_active: false,
  updated_at: '2026-10-30T12:00:00+00:00',
};

describe('classifyRiskWsMessage', () => {
  it('refetches on a risk event tick', () => {
    expect(classifyRiskWsMessage(riskEvent)).toEqual({ action: 'refetch' });
  });

  it('hands the heartbeat through on a risk state change', () => {
    expect(classifyRiskWsMessage(riskState)).toEqual({ action: 'heartbeat', beat: riskState });
  });

  it('keeps the nulls of a heartbeat with no established numbers', () => {
    const beat = classifyRiskWsMessage({
      ...riskState,
      equity: null,
      peak_equity: null,
      day_start_equity: null,
      daily_loss: null,
    });
    expect(beat).toEqual({
      action: 'heartbeat',
      beat: expect.objectContaining({ equity: null, daily_loss: null }),
    });
  });

  it('ignores subscription acks and foreign events', () => {
    expect(classifyRiskWsMessage({ ack: true, channel: 'risk' })).toEqual({ action: 'ignore' });
    expect(classifyRiskWsMessage({ event: 'order_fill', session_id: 's1' })).toEqual({
      action: 'ignore',
    });
  });

  it('ignores a malformed heartbeat rather than rendering its numbers', () => {
    expect(classifyRiskWsMessage({ event: 'risk_state' })).toEqual({ action: 'ignore' });
    expect(classifyRiskWsMessage({ ...riskState, equity: 'many' })).toEqual({ action: 'ignore' });
    expect(classifyRiskWsMessage({ ...riskState, current_drawdown: null })).toEqual({
      action: 'ignore',
    });
  });

  it('ignores malformed messages', () => {
    expect(classifyRiskWsMessage(null)).toEqual({ action: 'ignore' });
    expect(classifyRiskWsMessage('risk_event')).toEqual({ action: 'ignore' });
    expect(classifyRiskWsMessage(42)).toEqual({ action: 'ignore' });
  });
});
