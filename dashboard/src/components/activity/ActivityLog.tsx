import Link from 'next/link';
import { actionPhrase } from '@/lib/derive';
import { utc } from '@/lib/format';

export interface RiskEvent {
  id: number | null;
  ts: string | null;
  scope: string;
  kind: string;
  tier: number | null;
  actor: string;
  reason: string;
  details: Record<string, unknown>;
}

const ACTORS: Record<string, string> = {
  'user:soma': 'you',
  'service:api-token': 'API token',
  'service:mcp': 'Claude',
  risk_engine: 'engine (automatic)',
  'dev:dev': 'dev mode',
  anonymous: 'unknown caller',
};

export const actorLabel = (actor: string): string => ACTORS[actor] ?? actor;

const approvalAction = (details: Record<string, unknown>): string | null =>
  typeof details.action === 'string' && details.action ? details.action : null;

/** 06 §6: the event kind as a sentence; an unknown kind is shown verbatim, never hidden. */
export function eventSentence(e: Pick<RiskEvent, 'kind' | 'tier' | 'details'>, tierLabels: Record<number, string> = {}): string {
  const tier = e.tier ?? '?';
  switch (e.kind) {
    case 'kill_switch_on':
      return e.details.persisted === false ? 'Kill switch turned on; not saved' : 'Kill switch turned on';
    case 'kill_switch_off':
      return 'Kill switch released';
    case 'kill_switch_release_failed':
      return 'Kill switch release failed; it stayed on';
    case 'cb_escalate':
      return `Breaker moved to tier ${tier}${e.tier !== null && tierLabels[e.tier] ? `: ${tierLabels[e.tier]}` : ''}`;
    case 'cb_auto_reset':
      return `Breaker tier ${tier} reset automatically`;
    case 'cb_reset':
      return `Breaker tier ${tier} reset`;
    case 'cb_reset_failed':
      return 'Breaker reset failed; nothing changed';
    case 'daily_halt':
      return 'Daily loss halt started';
    case 'daily_halt_lifted':
      return 'Daily loss halt lifted';
    case 'consecutive_pause':
      return 'Loss-streak pause started';
    case 'consecutive_reset':
      return 'Loss-streak pause ended';
    case 'paper_reset':
      return 'Paper ledger reset';
    case 'flatten_failed':
      return 'Positions could not be closed';
    case 'session_carried':
      return 'Session P&L carried into the ledger';
    case 'approval_requested': {
      const action = approvalAction(e.details);
      return action ? `Asked to ${actionPhrase(action)}` : 'Approval requested';
    }
    case 'approval_approved': {
      const action = approvalAction(e.details);
      return action ? `Approved: ${actionPhrase(action)}` : 'Approval given';
    }
    case 'approval_denied': {
      const action = approvalAction(e.details);
      return action ? `Denied: ${actionPhrase(action)}` : 'Approval denied';
    }
    case 'approval_expired':
      return 'Approval expired';
    case 'approval_failed':
      return 'Approval failed';
    default:
      return e.kind;
  }
}

export interface RowLink {
  href: string;
  label: string;
}

interface ActivityLogProps {
  events: RiskEvent[];
  now: number;
  tierLabels?: Record<number, string>;
  /** Where the touched object lives, when the event names one; rendered as a link in the row. */
  rowLink?: (e: RiskEvent) => RowLink | null;
}

/** Newest first: time (UTC), what happened, who, why; the row opens to the raw kind and details. */
export function ActivityLog({ events, now, tierLabels, rowLink }: ActivityLogProps) {
  if (events.length === 0) {
    return <p className="text-sm text-ink-2">No risk events yet. Breaker changes, resets and kill-switch changes appear here.</p>;
  }
  return (
    <ul className="divide-y divide-line text-sm">
      {events.map((e, i) => {
        const link = rowLink?.(e) ?? null;
        return (
          <li key={e.id ?? i}>
            <details className="group py-2">
              <summary className="flex cursor-pointer flex-wrap gap-x-3 gap-y-1 max-lg:min-h-11 max-lg:items-center">
                <span className="font-mono text-ink-2">{utc(e.ts, now) ?? 'time not reported'}</span>
                <span className="font-medium text-ink-1">{eventSentence(e, tierLabels)}</span>
                <span className="text-ink-2">by {actorLabel(e.actor)}</span>
                {e.reason && <span className="text-ink-1">{e.reason}</span>}
                {link && (
                  <Link href={link.href} className="text-accent hover:underline">
                    {link.label} ›
                  </Link>
                )}
              </summary>
              <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs text-ink-2">
                <dt>Kind</dt>
                <dd className="font-mono text-ink-1">{e.kind}</dd>
                <dt>Details</dt>
                <dd>
                  <pre className="whitespace-pre-wrap break-words font-mono text-ink-1">{JSON.stringify(e.details)}</pre>
                </dd>
              </dl>
            </details>
          </li>
        );
      })}
    </ul>
  );
}
