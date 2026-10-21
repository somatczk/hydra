import { describe, expect, it } from 'vitest';
import { actorLabel, eventSentence } from './ActivityLog';

const ev = (kind: string, tier: number | null = null, details: Record<string, unknown> = {}) => ({ kind, tier, details });

describe('eventSentence', () => {
  it.each([
    [ev('kill_switch_on', null, { persisted: false }), 'Kill switch turned on; not saved'],
    [ev('kill_switch_on', null, { persisted: true }), 'Kill switch turned on'],
    [ev('kill_switch_off'), 'Kill switch released'],
    [ev('cb_reset', 2), 'Breaker tier 2 reset'],
    [ev('cb_auto_reset', 1), 'Breaker tier 1 reset automatically'],
    [ev('paper_reset'), 'Paper ledger reset'],
    [ev('never_seen_before'), 'never_seen_before'],
  ])('%j', (e, text) => {
    expect(eventSentence(e)).toBe(text);
  });

  it('names the tier label only when it is known', () => {
    expect(eventSentence(ev('cb_escalate', 3), { 3: 'Flatten all sessions' })).toBe('Breaker moved to tier 3: Flatten all sessions');
    expect(eventSentence(ev('cb_escalate', 3))).toBe('Breaker moved to tier 3');
  });
});

describe('actorLabel', () => {
  it('maps known actors and shows others verbatim', () => {
    expect(actorLabel('user:soma')).toBe('you');
    expect(actorLabel('service:api-token')).toBe('API token');
    expect(actorLabel('service:mcp')).toBe('service:mcp');
  });
});
