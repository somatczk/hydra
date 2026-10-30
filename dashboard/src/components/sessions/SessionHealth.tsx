import type { ReactNode } from 'react';
import { cn } from '@/components/ui/cn';
import { DataStateBadge } from '@/components/ui/DataStateBadge';
import { exchangeStopRow, isOverdue, isPerpSession, plural, reconcileRow, type SessionDetail } from '@/lib/derive';
import { duration, price, utc } from '@/lib/format';

type Tone = 'none' | 'caution' | 'danger';

function Row({ label, tone = 'none', children }: { label: string; tone?: Tone; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-4">
      <dt className="text-sm text-ink-2 sm:w-40 sm:shrink-0">{label}</dt>
      <dd className={cn('text-sm', tone === 'caution' && 'text-caution', tone === 'danger' && 'text-danger', tone === 'none' && 'text-ink-1')}>
        {children}
      </dd>
    </div>
  );
}

interface SessionHealthProps {
  detail: SessionDetail;
  /** Whether the session's push channel is connected; otherwise the page polls. */
  push: boolean;
  now: number;
}

/** What the engine knows about this session, in its own words: no "Stop armed" (05 §3, R-P1-1).
 * A live session adds the A7 reconcile row and reads its stops from the A8-live exchange verify. */
export function SessionHealth({ detail: d, push, now }: SessionHealthProps) {
  const running = d.status === 'running';
  const live = d.trading_mode === 'live';
  const overdue = running ? isOverdue(d.last_bar_at, d.timeframe, now) : null;
  const failures = d.protection_failures;
  const stops = d.open_orders.filter((o) => o.protective && o.stop_price !== null);
  // The A8-live rows are live-only by caller contract: a paper position's null stop is not a danger.
  const liveStops = live ? d.positions.map((p) => exchangeStopRow(p)) : null;
  const stopTone: Tone = liveStops?.some((r) => r?.tone === 'danger')
    ? 'danger'
    : liveStops?.some((r) => r === null)
      ? 'caution'
      : 'none';
  const reconcile = reconcileRow(d.reconcile, now);
  const started = d.started_at ? Date.parse(d.started_at) : NaN;
  const ended = d.stopped_at ? Date.parse(d.stopped_at) : now;

  return (
    <>
      <dl className="flex flex-col gap-2">
        <Row label="Status">
          {d.status}
          {Number.isNaN(started) ? '' : ` · ${duration((running ? now : ended) - started)}`}
        </Row>
        <Row label="Last bar" tone={overdue ? 'caution' : 'none'}>
          {d.last_bar_at
            ? `processed ${utc(d.last_bar_at, now)}${overdue === null ? '' : overdue ? ' · overdue' : ' · on time'}`
            : running
              ? `Waiting for the first closed ${d.timeframe} bar`
              : 'No bar processed'}
        </Row>
        <Row label="Entries" tone={d.entries_paused ? 'caution' : 'none'}>
          {d.entries_paused ?? 'Allowed'}
        </Row>
        <Row label="Stop failures" tone={failures.length > 0 ? 'caution' : 'none'}>
          {!running
            ? 'Stop failures: not tracked (session not running)'
            : failures.length === 0
              ? 'Stop failures: none since start'
              : `${plural(failures.length, 'stop failure')} since start. Last: ${failures[failures.length - 1]}`}
        </Row>
        {liveStops !== null && liveStops.length > 0 && (
          <Row label="Stops" tone={stopTone}>
            {/* S2: an unreadable verify names the server's error when it reported one. */}
            {d.positions.map((p, i) => liveStops[i]?.text ?? p.exchange_stop?.error ?? 'UNKNOWN').join(' · ')}
          </Row>
        )}
        {/* P7-G: a paper perp session's resting protective orders render as the Perps section's
            named units (the pair as ONE two-leg card), not as this row's unrelated stops. */}
        {!live && !isPerpSession(d) && stops.length > 0 && (
          <Row label="Stops">
            {stops.map((o) => `Stop at ${price(o.stop_price)} (${o.symbol})`).join(' · ')}
          </Row>
        )}
        {d.reconcile && (
          <Row label="Reconcile" tone={reconcile?.tone ?? 'caution'}>
            {reconcile ? reconcile.text : <DataStateBadge state="unknown" reason="The reconcile state couldn't be read" />}
          </Row>
        )}
        {running && d.feed_live === false && (
          <Row label="Feed" tone="caution">
            Feed not live
          </Row>
        )}
        <Row label="Saved" tone={d.persist_error || d.persist_dirty ? 'caution' : 'none'}>
          {[
            d.persist_error ? `Session status saved: no. ${d.persist_error}` : 'Session status saved: yes',
            d.persist_dirty ? 'Unsaved ledger changes' : null,
          ]
            .filter(Boolean)
            .join(' · ')}
        </Row>
        {d.error_message && (
          <Row label="Error" tone="danger">
            {d.error_message}
          </Row>
        )}
        <Row label="Market">
          {d.exchange_id} · {d.symbols.join(', ')} · {d.timeframe}
        </Row>
      </dl>
      {running && <p className="mt-2 text-sm text-ink-2">{push ? 'Push connected' : 'Polling every 15 s'}</p>}
    </>
  );
}
