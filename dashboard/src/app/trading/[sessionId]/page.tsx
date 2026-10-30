'use client';

import { useEffect, useState, type ReactNode } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { Copy } from 'lucide-react';
import { ActorChip } from '@/components/activity/ActorChip';
import { QuickTrade } from '@/components/sessions/QuickTrade';
import { SessionHealth } from '@/components/sessions/SessionHealth';
import { ModeBadge } from '@/components/shell/ModeBadge';
import { StopSessionDialog } from '@/components/sessions/StopSessionDialog';
import { useOutcomeSlot } from '@/components/sessions/useOutcomeSlot';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { ChartAndBook } from '@/components/trading/ChartAndBook';
import { Button } from '@/components/ui/Button';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { Table } from '@/components/ui/Table';
import { usePoll } from '@/hooks/usePoll';
import { connectWebSocket } from '@/lib/api';
import { stateFromError } from '@/lib/apiState';
import { exchangeStopRow, fillQty, isPerpSession, perpsRows, plural, sessionName, sideLabel, winRate, type PositionItem, type SessionDetail, type TradeItem } from '@/lib/derive';
import { money, pct, price, qty, signed, utc } from '@/lib/format';
import { baseAsset } from '@/lib/outcomes';
import { classifySessionWsMessage, debounce } from '@/lib/sessionWs';
import { PerpProtection } from '@/components/trading/PerpProtection';

const FILL_REFETCH_DEBOUNCE_MS = 500;

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3">
      <h2 id={id} className="text-base font-semibold text-ink-1">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Stat({ label, value, note }: { label: string; value: string | null; note?: string }) {
  return (
    <div className="rounded border border-line bg-surface p-3">
      <dt className="text-sm text-ink-2">{label}</dt>
      <dd className="mt-1 text-lg font-semibold text-ink-1">
        {value ?? (
          <>
            <span aria-hidden="true">—</span> <DataStateBadge state="unavailable" reason={`${label} isn't reported yet`} />
          </>
        )}
        {note && <p className="mt-0.5 text-xs font-normal text-ink-2">{note}</p>}
      </dd>
    </div>
  );
}

function Account({ d }: { d: SessionDetail }) {
  const m = d.metrics;
  const win = winRate(d.trades);
  return (
    <div className="flex flex-col gap-3">
      {d.trading_mode === 'live' && (
        <p className="text-sm font-semibold text-live">
          Real money on {d.exchange_id === 'bybit' ? 'Bybit' : 'Binance'}.
        </p>
      )}
      <dl className="grid grid-cols-2 gap-3 lg:grid-cols-3">
        <Stat label="Equity (USDT)" value={money(m.equity)} />
        <Stat label="Realised (USDT)" value={signed(m.realized_pnl)} />
        <Stat
          label="Unrealised (USDT)"
          value={signed(m.unrealized_pnl)}
          note={`Marked at the last closed ${d.timeframe} bar${d.last_bar_at ? ` (processed ${utc(d.last_bar_at)})` : ''}`}
        />
        <Stat label="Fees (USDT)" value={money(m.fees)} />
        <Stat label="Drawdown" value={pct(m.drawdown === null ? null : m.drawdown * 100)} />
        <div className="rounded border border-line bg-surface p-3">
          <dt className="text-sm text-ink-2">Win rate</dt>
          <dd className="mt-1 text-lg font-semibold text-ink-1">
            {win === null ? (
              <>
                <DataStateBadge state="no_data" reason="No closed trades yet" />{' '}
                <span className="text-sm font-normal text-ink-2">No closed trades yet</span>
              </>
            ) : (
              pct(win)
            )}
          </dd>
        </div>
      </dl>
    </div>
  );
}

/** P7-G: the hedge-capable display's per-side summary — count, the side's unrealised P&L,
 * and its total quantity only when the side's rows share one base (quantities across
 * different bases don't add). Longs first; a side renders only when it holds something. */
function sideSummaries(positions: PositionItem[]): Array<{ side: 'long' | 'short'; count: number; quantity: number; base: string | null; unrealizedPnl: number }> {
  const out: Array<{ side: 'long' | 'short'; count: number; quantity: number; base: string | null; unrealizedPnl: number }> = [];
  for (const side of ['long', 'short'] as const) {
    const rows = positions.filter((p) => (side === 'short' ? p.direction === 'short' : p.direction !== 'short'));
    if (rows.length === 0) continue;
    const bases = new Set(rows.map((p) => baseAsset(p.symbol)));
    out.push({
      side,
      count: rows.length,
      quantity: rows.reduce((n, p) => n + p.quantity, 0),
      base: bases.size === 1 ? [...bases][0] : null,
      unrealizedPnl: rows.reduce((n, p) => n + p.unrealized_pnl, 0),
    });
  }
  return out;
}

function Positions({ d }: { d: SessionDetail }) {
  const base = baseAsset(d.symbols[0] ?? '');
  const live = d.trading_mode === 'live';
  const perp = isPerpSession(d);
  const sides = perp ? sideSummaries(d.positions) : [];
  if (d.positions.length > 0) {
    return (
      <>
        {sides.length > 0 && (
          <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
            {sides.map((s) => (
              <div key={s.side} className="flex items-center gap-2 rounded border border-line bg-surface p-2">
                <StatusBadge status={s.side === 'long' ? 'LONG' : 'SHORT'} variant={s.side === 'long' ? 'success' : 'error'} size="sm" />
                <p className="text-sm text-ink-1">
                  {plural(s.count, 'position')}
                  {s.base !== null ? ` · ${qty(s.quantity)} ${s.base}` : ''}
                  {` · unrealised ${signed(s.unrealizedPnl)} USDT`}
                </p>
              </div>
            ))}
          </div>
        )}
        <Table<PositionItem>
        columns={[
          { key: 'symbol', header: 'Symbol' },
          {
            key: 'direction',
            header: 'Side',
            render: (p) =>
              perp ? (
                <StatusBadge status={p.direction === 'short' ? 'SHORT' : 'LONG'} variant={p.direction === 'short' ? 'error' : 'success'} size="sm" />
              ) : (
                sideLabel(p.direction).text
              ),
          },
          // A perp SHORT renders its quantity signed (it closes by buying back); spot stays unsigned.
          { key: 'quantity', header: `Qty (${base})`, render: (p) => (p.direction === 'short' ? `-${qty(p.quantity)}` : qty(p.quantity)) },
          { key: 'avg_entry_price', header: 'Avg entry', render: (p) => price(p.avg_entry_price) },
          { key: 'unrealized_pnl', header: 'Unrealised (USDT)', render: (p) => signed(p.unrealized_pnl) },
          // The A8-live cell is live-only: a paper position's null exchange_stop is not a danger (R26's note).
          ...(live
            ? [
                {
                  key: 'exchange_stop',
                  header: 'Exchange stop',
                  render: (p: PositionItem) => {
                    const r = exchangeStopRow(p);
                    return r ? (
                      <span className={r.tone === 'danger' ? 'text-danger' : r.tone === 'caution' ? 'text-caution' : 'text-ink-1'}>
                        {r.text}
                      </span>
                    ) : (
                      // S2: an unreadable verify names the server's error when it reported one.
                      <DataStateBadge state="unknown" reason={p.exchange_stop?.error ?? "The exchange stop couldn't be verified"} />
                    );
                  },
                },
              ]
            : []),
        ]}
        data={d.positions}
        keyExtractor={(p) => p.symbol}
      />
        {perp && d.positions.some((p) => p.direction === 'short') && (
          <p className="mt-2 text-sm text-ink-2">
            {"A short closes by buying it back. Its strategy's exit rules and the kill switch close it; manual orders are long-only in 2.8.0."}
          </p>
        )}
      </>
    );
  }
  if (d.status !== 'running' && (d.positions_at_stop > 0 || d.flatten_incomplete)) {
    const held = d.positions_at_stop > 0 ? plural(d.positions_at_stop, 'position') : 'Positions';
    return <p className="text-sm text-danger">{held} still held; details aren&apos;t available for a stopped session.</p>;
  }
  return <p className="text-sm text-ink-2">No open positions. This session holds only USDT.</p>;
}

/** P4-C's perp session reads (the detail's `perps` block): the margin wallet, leverage and
 * isolated mode, the funding accrual with its skips visible, and the liquidation state.
 * P7-G adds the one-line mode facts. */
function Perps({ d }: { d: SessionDetail }) {
  const rows = perpsRows(d.perps);
  const perps = d.perps;
  if (!rows || !perps) return null;
  // P7-G: the mode facts in one muted line — one-way position mode and isolated margin are
  // the venue setup the session manager enforces before any order; the leverage is the
  // strategy's own pick within the 1-3× platform cap.
  const mode = `One-way position mode · Isolated margin · the strategy's leverage ${perps.leverage}× (1-3×)`;
  return (
    <>
      <p className="mb-3 text-sm text-ink-2">{mode}</p>
      <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="rounded border border-line bg-surface p-3">
          <dt className="text-sm text-ink-2">Leverage</dt>
          <dd className="mt-1 text-lg font-semibold text-ink-1">{rows.leverage}</dd>
        </div>
        <div className="rounded border border-line bg-surface p-3">
          <dt className="text-sm text-ink-2">Margin wallet (USDT)</dt>
          <dd className="mt-1 text-lg font-semibold text-ink-1">{rows.wallet}</dd>
        </div>
        <div className="rounded border border-line bg-surface p-3 sm:col-span-2">
          <dt className="text-sm text-ink-2">Funding</dt>
          <dd className={rows.funding.tone === 'caution' ? 'text-sm text-caution' : 'text-sm text-ink-1'}>{rows.funding.text}</dd>
        </div>
        {rows.liquidations && (
          <div className="rounded border border-line bg-surface p-3 sm:col-span-2">
            <dt className="text-sm text-ink-2">Liquidations</dt>
            <dd className="text-sm font-semibold text-danger">{rows.liquidations.text}</dd>
          </div>
        )}
      </dl>
    </>
  );
}

function Fills({ d }: { d: SessionDetail }) {
  if (d.trades.length === 0) {
    return <p className="text-sm text-ink-2">No fills yet. The strategy trades on closed {d.timeframe} bars.</p>;
  }
  return (
    <Table<TradeItem>
      columns={[
        { key: 'timestamp', header: 'Time (UTC)', render: (t) => utc(t.timestamp) ?? '—' },
        { key: 'side', header: 'Side', render: (t) => sideLabel(t.side).text },
        {
          key: 'quantity',
          header: 'Qty',
          render: (t) => {
            const q = fillQty(t);
            return q === null ? (
              <>
                <span aria-hidden="true">—</span> <span className="sr-only">quantity not reported</span>
                <DataStateBadge state="unknown" />
              </>
            ) : (
              qty(q)
            );
          },
        },
        { key: 'price', header: 'Price', render: (t) => price(t.price) ?? '—' },
        { key: 'fee', header: 'Fee', render: (t) => money(t.fee) ?? '—' },
        { key: 'net_pnl', header: 'Net P&L', render: (t) => signed(t.net_pnl) ?? '—' },
        { key: 'reason', header: 'Reason', render: (t) => t.reason ?? '—' },
        // R20: only a live fill carries `reconciled`; the label flags a fill no reconcile pass has covered.
        ...(d.trading_mode === 'live'
          ? [
              {
                key: 'reconciled',
                header: 'Reconciled',
                render: (t: TradeItem) => (t.reconciled === false ? <span className="text-caution">not reconciled</span> : null),
              },
            ]
          : []),
      ]}
      data={d.trades}
      keyExtractor={(t) => t.id ?? `${t.timestamp}-${t.price}`}
    />
  );
}

export default function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const shell = useShellStatus();
  const poll = usePoll<SessionDetail>(`/api/trading/sessions/${sessionId}/detail`, 15_000);
  const { data: d, refetch } = poll;
  const [push, setPush] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [copied, setCopied] = useState(false);
  const running = d?.status === 'running';
  const row = shell.sessions.data?.find((s) => s.session_id === sessionId);
  const listed = { persist_dirty: row?.persist_dirty ?? null, feed_live: row?.feed_live ?? null };
  const { refetchAll } = shell;

  const slot = useOutcomeSlot(
    () => (d ? sessionName(d) : sessionId),
    () => {
      void refetch();
      void refetchAll();
    },
  );

  useEffect(() => {
    if (d) document.title = `Session ${sessionName(d)} · Hydra`;
  }, [d]);

  useEffect(() => {
    if (!running) return;
    const refetchOnFill = debounce(() => void refetch(), FILL_REFETCH_DEBOUNCE_MS);
    const ws = connectWebSocket(`/ws/trades?session_id=${sessionId}`, (msg) => {
      if (classifySessionWsMessage(msg, sessionId) === 'refetch') refetchOnFill.trigger();
    });
    if (ws) {
      ws.onopen = () => setPush(true);
      ws.onclose = () => setPush(false);
      ws.onerror = () => setPush(false);
    }
    return () => {
      refetchOnFill.cancel();
      ws?.close();
      setPush(false);
    };
  }, [running, sessionId, refetch]);

  if (d === null) {
    if (poll.state === 'loading') return <div className="h-48 animate-pulse rounded border border-line bg-surface" aria-label="Loading session" />;
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-xl font-semibold">{stateFromError(poll.error) === 'not_found' ? 'Session not found' : 'Session'}</h1>
        <DataStateErrorCard error={poll.error} fallback="Failed to load the session" onRetry={() => void refetch()} />
        <Link href="/trading" className="text-sm underline">
          Back to Sessions
        </Link>
      </div>
    );
  }

  const name = sessionName(d);
  const id8 = d.session_id.slice(0, 8);
  const holds = d.status !== 'running' && (d.positions_at_stop > 0 || d.flatten_incomplete);
  const copy = () =>
    navigator.clipboard
      .writeText(d.session_id)
      .then(() => setCopied(true))
      .catch(() => setCopied(false));

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-2">
        <nav aria-label="Breadcrumb" className="text-sm text-ink-2 max-lg:hidden">
          <Link href="/trading" className="underline">
            Sessions
          </Link>{' '}
          / {name} · <span className="font-mono">{id8}</span>
        </nav>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold text-ink-1">{name}</h1>
          {d.trading_mode === 'live' && <ModeBadge mode="live" />}
          <StatusBadge status={d.status} variant={d.status === 'error' ? 'error' : d.status === 'running' ? 'info' : 'neutral'} />
          <ActorChip actor={d.created_by} />
          {poll.state === 'stale' && poll.fetchedAt !== null && <DataStateBadge state="stale" reason={`Session as of ${utc(poll.fetchedAt)}`} />}
          {running && (
            <Button variant="secondary" onClick={() => setStopping(true)}>
              Stop session…
            </Button>
          )}
        </div>
        <p className="flex items-center gap-2 text-sm text-ink-2">
          <span className="font-mono">{id8}</span>
          <button
            type="button"
            aria-label={`Copy session id ${d.session_id}`}
            onClick={() => void copy()}
            className="inline-flex h-8 w-8 items-center justify-center rounded hover:bg-hover max-lg:h-11 max-lg:w-11"
          >
            <Copy className="h-4 w-4" aria-hidden="true" />
          </button>
          {copied && <span role="status">Copied</span>}
        </p>
      </header>

      {slot.panel}

      <Section id="health-heading" title="Health">
        <SessionHealth detail={{ ...d, ...listed }} push={push} now={shell.now} />
      </Section>

      <Section id="account-heading" title="Account">
        <Account d={d} />
      </Section>

      {d.perps && (
        <Section id="perps-heading" title="Perps">
          <Perps d={d} />
          {/* P7-G: a paper perp session's resting protective orders render here as units
              (the NO-OCO pair as ONE two-leg card); live perps read stops via the A8 verify. */}
          {isPerpSession(d) && d.trading_mode !== 'live' && <PerpProtection orders={d.open_orders} />}
        </Section>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Section id="positions-heading" title="Positions">
          <Positions d={d} />
          {holds && (
            <div>
              <Button
                variant="secondary"
                disabled={slot.busy !== null}
                onClick={() => void slot.again({ kind: 'try_closing', label: 'Try closing again', sessionIds: [d.session_id] })}
              >
                Try closing again
              </Button>
            </div>
          )}
        </Section>
        <ChartAndBook symbol={d.symbols[0] ?? 'BTCUSDT'} exchangeId={d.exchange_id} />
      </div>

      {running && (
        <Section id="quick-trade-heading" title="Trade in this session">
          <QuickTrade
            session={d}
            positions={d.positions}
            onOutcome={slot.setOutcome}
            onSent={() => {
              void refetch();
              void refetchAll();
            }}
          />
        </Section>
      )}

      <Section id="fills-heading" title="Fills">
        <Fills d={d} />
      </Section>

      {stopping && (
        <StopSessionDialog
          session={d}
          positions={d.positions}
          open
          onClose={() => setStopping(false)}
          onStop={() => slot.stop(d.session_id)}
        />
      )}
    </div>
  );
}
