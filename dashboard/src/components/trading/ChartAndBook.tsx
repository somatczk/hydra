'use client';

import { useState } from 'react';
import { useTheme } from '@/components/layout/ThemeProvider';
import { DataCard } from '@/components/ui/DataCard';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { usePoll } from '@/hooks/usePoll';
import { isStale } from '@/lib/apiState';
import { price, qty, utc } from '@/lib/format';

interface OrderBook {
  bids: [number, number][];
  asks: [number, number][];
  spread: number;
  timestamp: string | null;
  fetched_at: string;
}

const ORDER_BOOK_MAX_AGE_MS = 15_000;
const DEPTH = 5;

/** The session's exchange -> TradingView symbol prefix (bybit joins live-only, P7-F). */
const TRADING_VIEW_PREFIX: Record<string, string> = {
  binance: 'BINANCE',
  binanceusdm: 'BINANCE',
  bybit: 'BYBIT',
};

/** Cross-origin TradingView widget: no third-party script runs in this origin (DB-17). */
export function tradingViewSrc(
  symbol: string,
  theme: 'dark' | 'light',
  exchangeId: string = 'binance',
): string {
  const venue = TRADING_VIEW_PREFIX[exchangeId] ?? 'BINANCE';
  const params = new URLSearchParams({
    symbol: `${venue}:${symbol}`,
    interval: '60',
    theme,
    style: '1',
    locale: 'en',
    timezone: 'Etc/UTC',
  });
  return `https://s.tradingview.com/widgetembed/?${params.toString()}`;
}

function Book({ symbol }: { symbol: string }) {
  const book = usePoll<OrderBook>(`/api/market/orderbook?symbol=${symbol}`, 10_000);
  if (book.data === null) {
    return book.state === 'loading' ? (
      <p className="text-sm text-ink-2">Loading order book…</p>
    ) : (
      <DataStateErrorCard error={book.error} fallback="Order book unavailable" onRetry={() => void book.refetch()} />
    );
  }
  const b = book.data;
  const late = book.state === 'stale' || (b.timestamp !== null && isStale(b.timestamp, ORDER_BOOK_MAX_AGE_MS));
  const side = (label: string, rows: [number, number][]) => (
    <div className="flex-1">
      <p className="text-xs font-semibold text-ink-2">{label}</p>
      <ul className="font-mono text-sm">
        {rows.slice(0, DEPTH).map(([p, q]) => (
          <li key={p} className="flex justify-between gap-4">
            <span>{price(p)}</span>
            <span className="text-ink-2">{qty(q)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2 text-xs text-ink-2">
        <span>Spread {price(b.spread)}</span>
        <span>Fetched {utc(b.fetched_at)}</span>
        {late && <DataStateBadge state="stale" reason={`Order book as of ${utc(b.timestamp ?? b.fetched_at)}`} />}
      </div>
      <div className="flex gap-6">
        {side('Bids', b.bids)}
        {side('Asks', b.asks)}
      </div>
    </div>
  );
}

/** The session's market: TradingView chart and Hydra's order book; behind a button below 1024 px. */
export function ChartAndBook({
  symbol,
  exchangeId = 'binance',
}: {
  symbol: string;
  exchangeId?: string;
}) {
  const { resolved } = useTheme();
  const [open, setOpen] = useState(false);
  return (
    <section aria-labelledby="chart-heading" className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 id="chart-heading" className="text-base font-semibold text-ink-1">
          Chart and order book
        </h2>
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="h-11 rounded border border-line-strong px-4 text-sm font-medium hover:bg-hover lg:hidden"
        >
          {open ? 'Hide chart' : 'Show chart'}
        </button>
      </div>
      <div className={open ? 'flex flex-col gap-4' : 'hidden flex-col gap-4 lg:flex'}>
        <DataCard
          title={symbol}
          description={`TradingView ${exchangeId} data from TradingView, not Hydra's stored bars`}
        >
          <iframe
            key={resolved}
            title={`TradingView ${symbol} chart`}
            src={tradingViewSrc(symbol, resolved, exchangeId)}
            className="h-[28rem] w-full rounded border-0"
            sandbox="allow-scripts allow-same-origin allow-popups"
            referrerPolicy="no-referrer"
            loading="lazy"
          />
        </DataCard>
        <DataCard title="Order book">
          <Book symbol={symbol} />
        </DataCard>
      </div>
    </section>
  );
}
