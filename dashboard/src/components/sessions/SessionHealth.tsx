import type { ReactNode } from 'react';
import { cn } from '@/components/ui/cn';
import { isOverdue, plural, type SessionDetail } from '@/lib/derive';
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

/** What the engine knows about this session, in its own words: no "Live", no "Stop armed" (05 §3, R-P1-1). */
export function SessionHealth({ detail: d, push, now }: SessionHealthProps) {
  const running = d.status === 'running';
  const overdue = running ? isOverdue(d.last_bar_at, d.timeframe, now) : null;
  const failures = d.protection_failures;
  const stops = d.open_orders.filter((o) => o.protective && o.stop_price !== null);
  const started = d.started_at ? Date.parse(d.started_at) : NaN;
  const ended = d.stopped_at ? Date.parse(d.stopped_at) : now;

  return (
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
      {stops.length > 0 && (
        <Row label="Stops">
          {stops.map((o) => `Stop at ${price(o.stop_price)} (${o.symbol})`).join(' · ')}
        </Row>
      )}
      <Row label="Saved" tone={d.persist_error ? 'caution' : 'none'}>
        {d.persist_error ? `Session status saved: no. ${d.persist_error}` : 'Session status saved: yes'}
      </Row>
      {d.error_message && (
        <Row label="Error" tone="danger">
          {d.error_message}
        </Row>
      )}
      <Row label="Market">
        {d.exchange_id} · {d.symbols.join(', ')} · {d.timeframe}
      </Row>
      {running && (
        <p className="text-sm text-ink-2">{push ? 'Push connected' : 'Polling every 15 s'}</p>
      )}
    </dl>
  );
}
