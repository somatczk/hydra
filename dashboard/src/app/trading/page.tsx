'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowUpDown, Eye, OctagonX, Shield, RotateCcw, Zap } from 'lucide-react';
import { DataCard } from '@/components/ui/DataCard';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { ErrorCard } from '@/components/ui/ErrorCard';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { useTheme } from '@/components/layout/ThemeProvider';
import { logger } from '@/lib/logger';

/* ---------- Types ---------- */

interface Position {
  id: string;
  pair: string;
  side: 'Long' | 'Short';
  size: string;
  entry: string;
  current: string;
  pnl: string;
  pnlPercent: string;
}

interface RecentTrade {
  id: string;
  pair: string;
  side: 'Long' | 'Short';
  size: string;
  price: string;
  fee: string;
  pnl: number;
  pnlFmt: string;
  time: string;
}

interface ApiPosition {
  id: string;
  pair: string;
  side: string;
  size: number;
  entry_price: number;
  current_price: number;
  unrealized_pnl: number;
  pnl_pct: number;
}

interface ApiRecentTrade {
  id: string;
  symbol: string;
  side: string;
  price: number;
  quantity: number;
  fee: number;
  pnl: number;
  timestamp: string;
}

interface TradingSession {
  session_id: string;
  strategy_id: string;
  strategy_name: string | null;
  trading_mode: string;
  status: string;
  symbols: string[];
  timeframe: string;
  started_at: string | null;
}

interface RiskConfig {
  kill_switch_active: boolean;
}

interface CircuitBreaker {
  tier: number;
  label: string;
  threshold: string;
  current_value: number;
  status: string;
}

interface RiskStatus {
  current_drawdown: number;
  max_drawdown_limit: number;
  daily_loss: number;
  daily_loss_limit: number;
  circuit_breakers: CircuitBreaker[];
}

interface OrderBook {
  bids: [number, number][];
  asks: [number, number][];
}

interface FundingRate {
  symbol: string;
  rate: number;
  next_funding_time: string | null;
  annualized_rate: number;
}

/* ---------- Helpers ---------- */

function mapApiPositions(data: ApiPosition[]): Position[] {
  const fmt = (v: number) => `$${v.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
  return data.map((p) => ({
    id: p.id,
    pair: p.pair,
    side: p.side as 'Long' | 'Short',
    size: `${p.size} BTC`,
    entry: fmt(p.entry_price),
    current: fmt(p.current_price),
    pnl: `${p.unrealized_pnl >= 0 ? '+' : ''}${fmt(p.unrealized_pnl)}`,
    pnlPercent: `${p.pnl_pct >= 0 ? '+' : ''}${p.pnl_pct.toFixed(2)}%`,
  }));
}

function formatSymbol(symbol: string): string {
  const match = symbol.match(/^([A-Z]{3,4})(USDT|USD|BUSD|USDC)$/);
  if (match) return `${match[1]}/${match[2]}`;
  return symbol;
}

function mapApiTrades(data: ApiRecentTrade[]): RecentTrade[] {
  return data.map((t) => ({
    id: t.id,
    pair: formatSymbol(t.symbol),
    side: t.side === 'BUY' ? 'Long' as const : 'Short' as const,
    size: `${t.quantity.toLocaleString('en-US')} BTC`,
    price: `$${t.price.toLocaleString('en-US', { minimumFractionDigits: 2 })}`,
    fee: t.fee > 0 ? `$${t.fee.toFixed(4)}` : '\u2014',
    pnl: t.pnl,
    pnlFmt: `${t.pnl >= 0 ? '+' : ''}$${Math.abs(t.pnl).toLocaleString('en-US', { minimumFractionDigits: 2 })}`,
    time: new Date(t.timestamp).toLocaleString('en-US', { dateStyle: 'short', timeStyle: 'medium' }),
  }));
}

const positionColumns = [
  { key: 'pair', header: 'Pair' },
  {
    key: 'side',
    header: 'Side',
    render: (row: Position) => (
      <StatusBadge
        status={row.side}
        variant={row.side === 'Long' ? 'success' : 'error'}
        size="sm"
      />
    ),
  },
  { key: 'size', header: 'Size' },
  { key: 'entry', header: 'Entry', hideOnMobile: true },
  { key: 'current', header: 'Current', hideOnMobile: true },
  {
    key: 'pnl',
    header: 'PnL',
    render: (row: Position) => (
      <div>
        <span
          className={
            row.pnl.startsWith('+')
              ? 'text-status-success font-medium'
              : 'text-status-error font-medium'
          }
        >
          {row.pnl}
        </span>
        <span className="ml-1 text-xs text-text-muted">{row.pnlPercent}</span>
      </div>
    ),
  },
];

const tradeHistoryColumns = [
  { key: 'pair', header: 'Pair' },
  {
    key: 'side',
    header: 'Side',
    render: (row: RecentTrade) => (
      <StatusBadge
        status={row.side}
        variant={row.side === 'Long' ? 'success' : 'error'}
        size="sm"
      />
    ),
  },
  { key: 'size', header: 'Size' },
  { key: 'price', header: 'Price' },
  { key: 'fee', header: 'Fee', hideOnMobile: true },
  {
    key: 'pnl',
    header: 'PnL',
    render: (row: RecentTrade) => (
      <span
        className={
          row.pnl >= 0
            ? 'text-status-success font-medium'
            : 'text-status-error font-medium'
        }
      >
        {row.pnlFmt}
      </span>
    ),
  },
  { key: 'time', header: 'Time', hideOnMobile: true },
];

/* ---------- Page ---------- */

export default function TradingPage() {
  const router = useRouter();
  const { toast } = useToast();
  useEffect(() => { logger.info('Trading', 'Page mounted'); }, []);
  const [openPositions, setOpenPositions] = useState<Position[] | null>(null);
  const [recentTrades, setRecentTrades] = useState<RecentTrade[] | null>(null);
  const [sessions, setSessions] = useState<TradingSession[]>([]);
  const [killSwitchActive, setKillSwitchActive] = useState(false);
  const [killSwitchLoading, setKillSwitchLoading] = useState(false);
  const [circuitBreakers, setCircuitBreakers] = useState<CircuitBreaker[] | null>(null);
  const [riskStatus, setRiskStatus] = useState<RiskStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchErrors, setFetchErrors] = useState<Record<string, boolean>>({});
  const [resettingTier, setResettingTier] = useState<number | null>(null);
  const [orderBook, setOrderBook] = useState<OrderBook | null>(null);
  const [fundingRate, setFundingRate] = useState<FundingRate | null>(null);

  // Quick trade form state
  const [qtSymbol, setQtSymbol] = useState('BTCUSDT');
  const [qtSide, setQtSide] = useState<'buy' | 'sell'>('buy');
  const [qtQuantity, setQtQuantity] = useState('');
  const [qtOrderType, setQtOrderType] = useState('market');
  const [qtPrice, setQtPrice] = useState('');
  const [qtTakeProfit, setQtTakeProfit] = useState('');
  const [qtStopLoss, setQtStopLoss] = useState('');
  const [qtSubmitting, setQtSubmitting] = useState(false);

  const { resolved: themeResolved } = useTheme();
  const widgetContainerRef = useRef<HTMLDivElement>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setFetchErrors({});

    const cfg = await fetchApi<{ trading_mode: string }>('/api/system/config')
      .catch(() => ({ trading_mode: 'paper' }));
    const source = cfg.trading_mode === 'live' ? 'live' : 'paper';
    const qs = `?source=${source}`;

    await Promise.all([
      fetchApi<ApiPosition[]>(`/api/portfolio/positions${qs}`)
        .then((data) => setOpenPositions(mapApiPositions(data)))
        .catch(() => { setFetchErrors((prev) => ({ ...prev, positions: true })); }),
      fetchApi<{ trades: ApiRecentTrade[] } | ApiRecentTrade[]>(`/api/portfolio/trades${qs}`)
        .then((data) => {
          const trades = Array.isArray(data) ? data : (data.trades ?? []);
          setRecentTrades(mapApiTrades(trades));
        })
        .catch(() => { setFetchErrors((prev) => ({ ...prev, trades: true })); }),
      fetchApi<TradingSession[]>('/api/trading/sessions')
        .then((data) => setSessions(data.filter((s) => s.status === 'running')))
        .catch(() => { setFetchErrors((prev) => ({ ...prev, sessions: true })); }),
      fetchApi<RiskConfig>('/api/risk/config')
        .then((cfg) => setKillSwitchActive(cfg.kill_switch_active))
        .catch(() => { setFetchErrors((prev) => ({ ...prev, riskConfig: true })); }),
      fetchApi<CircuitBreaker[]>('/api/risk/circuit-breakers')
        .then(setCircuitBreakers)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, circuitBreakers: true })); }),
      fetchApi<RiskStatus>('/api/risk/status')
        .then(setRiskStatus)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, riskStatus: true })); }),
      fetchApi<OrderBook>('/api/market/orderbook?symbol=BTCUSDT')
        .then(setOrderBook)
        .catch(() => { setFetchErrors((prev) => ({ ...prev, orderBook: true })); }),
      fetchApi<FundingRate[]>('/api/market/funding-rates/current?symbol=BTCUSDT')
        .then((arr) => setFundingRate(arr?.[0] ?? null))
        .catch(() => { setFetchErrors((prev) => ({ ...prev, fundingRate: true })); }),
    ]);

    setLoading(false);
  }, []);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 15_000);
    return () => clearInterval(interval);
  }, [loadData]);

  // Order book auto-refresh every 5s
  useEffect(() => {
    const interval = setInterval(() => {
      fetchApi<OrderBook>('/api/market/orderbook?symbol=BTCUSDT')
        .then(setOrderBook)
        .catch(() => {});
    }, 5_000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    const container = widgetContainerRef.current;
    if (!container) return;

    const script = document.createElement('script');
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
    script.type = 'text/javascript';
    script.async = true;
    script.innerHTML = JSON.stringify({
      autosize: true,
      symbol: 'BINANCE:BTCUSDT',
      interval: '60',
      timezone: 'Etc/UTC',
      theme: themeResolved === 'dark' ? 'dark' : 'light',
      style: '1',
      locale: 'en',
      allow_symbol_change: true,
      support_host: 'https://www.tradingview.com',
    });

    container.appendChild(script);

    return () => {
      container.innerHTML = '';
    };
  }, [themeResolved]);

  const handleToggleKillSwitch = async () => {
    setKillSwitchLoading(true);
    try {
      if (killSwitchActive) {
        await fetchApi('/api/trading/kill-switch', { method: 'DELETE' });
        setKillSwitchActive(false);
        toast('success', 'Kill switch deactivated');
      } else {
        await fetchApi('/api/trading/kill-switch', { method: 'POST' });
        setKillSwitchActive(true);
        toast('warning', 'Kill switch activated — all trading halted');
      }
    } catch (err) {
      logger.error('Trading', 'Failed to toggle kill switch', err);
      toast('error', 'Failed to toggle kill switch');
    } finally {
      setKillSwitchLoading(false);
    }
  };

  const handleResetCircuitBreaker = async (tier: number) => {
    setResettingTier(tier);
    try {
      await fetchApi(`/api/risk/circuit-breakers/${tier}/reset`, { method: 'POST' });
      toast('success', `Circuit breaker tier ${tier} reset`);
      loadData();
    } catch (err) {
      logger.error('Trading', `Failed to reset circuit breaker tier ${tier}`, err);
      toast('error', `Failed to reset tier ${tier}`);
    } finally {
      setResettingTier(null);
    }
  };

  const handleQuickTrade = async () => {
    if (!qtQuantity) {
      toast('error', 'Quantity is required');
      return;
    }
    setQtSubmitting(true);
    try {
      const payload: Record<string, unknown> = {
        symbol: qtSymbol,
        side: qtSide,
        quantity: parseFloat(qtQuantity),
        order_type: qtOrderType,
      };
      if (qtOrderType === 'limit' && qtPrice) payload.price = parseFloat(qtPrice);
      if (qtTakeProfit) payload.take_profit = parseFloat(qtTakeProfit);
      if (qtStopLoss) payload.stop_loss = parseFloat(qtStopLoss);

      await fetchApi('/api/trading/quick-trade', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      toast('success', `${qtSide.toUpperCase()} order placed for ${qtQuantity} ${qtSymbol}`);
      setQtQuantity('');
      setQtPrice('');
      setQtTakeProfit('');
      setQtStopLoss('');
    } catch (err) {
      logger.error('Trading', 'Quick trade failed', err);
      toast('error', 'Failed to place order');
    } finally {
      setQtSubmitting(false);
    }
  };

  const runningSessions = sessions.filter((s) => s.status === 'running');

  return (
    <div className="flex flex-col gap-6">
      {/* Kill switch banner */}
      {killSwitchActive && (
        <div className="rounded-xl border-2 border-status-error bg-status-error/10 p-4 flex items-center gap-3">
          <OctagonX className="h-6 w-6 text-status-error shrink-0" />
          <div>
            <p className="text-sm font-semibold text-status-error">Kill Switch Active</p>
            <p className="text-xs text-text-muted">
              All trading is halted. Release the kill switch to resume.
            </p>
          </div>
        </div>
      )}

      {/* Risk Controls */}
      <DataCard title="Risk Controls" description="Kill switch, circuit breakers, and daily PnL">
        <div className="space-y-4">
          {/* Kill switch toggle */}
          <div className="flex items-center justify-between rounded-lg border border-border-default bg-bg-secondary p-3">
            <div className="flex items-center gap-3">
              <Shield className="h-4 w-4 text-text-muted shrink-0" />
              <div>
                <p className="text-sm font-medium text-text-primary">Kill Switch</p>
                <p className="text-xs text-text-muted">Immediately halt all trading activity</p>
              </div>
            </div>
            <Button
              variant={killSwitchActive ? 'primary' : 'danger'}
              size="sm"
              onClick={handleToggleKillSwitch}
              loading={killSwitchLoading}
            >
              {killSwitchActive ? 'Deactivate' : 'Activate'}
            </Button>
          </div>

          {/* Daily PnL vs limit */}
          {riskStatus && (
            <div className="flex items-center justify-between rounded-lg border border-border-default bg-bg-secondary p-3">
              <div>
                <p className="text-sm font-medium text-text-primary">Daily PnL vs Limit</p>
                <p className="text-xs text-text-muted">
                  Loss: ${Math.abs(riskStatus.daily_loss).toFixed(2)} / Limit: ${riskStatus.daily_loss_limit.toFixed(2)}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <div className="w-32 h-2 rounded-full bg-bg-tertiary overflow-hidden">
                  <div
                    className="h-full rounded-full bg-status-error transition-all"
                    style={{ width: `${Math.min((Math.abs(riskStatus.daily_loss) / (riskStatus.daily_loss_limit || 1)) * 100, 100)}%` }}
                  />
                </div>
                <span className="text-xs text-text-muted">
                  {((Math.abs(riskStatus.daily_loss) / (riskStatus.daily_loss_limit || 1)) * 100).toFixed(0)}%
                </span>
              </div>
            </div>
          )}

          {/* Circuit breakers */}
          {fetchErrors.circuitBreakers ? (
            <ErrorCard message="Failed to load circuit breakers" onRetry={loadData} />
          ) : circuitBreakers && circuitBreakers.length > 0 ? (
            <div className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-wider text-text-muted">Circuit Breakers</p>
              {circuitBreakers.map((cb) => (
                <div key={cb.tier} className="flex items-center justify-between rounded-lg border border-border-default bg-bg-secondary p-3">
                  <div className="flex items-center gap-3">
                    <StatusBadge
                      status={cb.status === 'tripped' ? 'Tripped' : 'OK'}
                      variant={cb.status === 'tripped' ? 'error' : 'success'}
                      size="sm"
                    />
                    <div>
                      <p className="text-sm font-medium text-text-primary">Tier {cb.tier}: {cb.label}</p>
                      <p className="text-xs text-text-muted">Threshold: {cb.threshold} | Current: {cb.current_value}</p>
                    </div>
                  </div>
                  {cb.status === 'tripped' && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => handleResetCircuitBreaker(cb.tier)}
                      loading={resettingTier === cb.tier}
                    >
                      <RotateCcw className="h-3 w-3" />
                      Reset
                    </Button>
                  )}
                </div>
              ))}
            </div>
          ) : null}

          {/* Funding rate */}
          {fetchErrors.fundingRate ? (
            <ErrorCard message="Failed to load funding rate" onRetry={loadData} />
          ) : fundingRate ? (
            <div className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-wider text-text-muted">Funding Rate</p>
              <div className="flex items-center justify-between rounded-lg border border-border-default bg-bg-secondary p-3">
                <div>
                  <p className="text-sm font-medium text-text-primary">{fundingRate.symbol}</p>
                  <p className="text-xs text-text-muted">
                    Next: {fundingRate.next_funding_time ? new Date(fundingRate.next_funding_time).toLocaleTimeString() : 'N/A'}
                  </p>
                </div>
                <div className="text-right">
                  <p className={`text-sm font-semibold ${fundingRate.rate >= 0 ? 'text-status-success' : 'text-status-error'}`}>
                    {fundingRate.rate >= 0 ? '+' : ''}{(fundingRate.rate * 100).toFixed(4)}%
                  </p>
                  <p className="text-xs text-text-muted">
                    {(fundingRate.annualized_rate).toFixed(2)}% ann.
                  </p>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </DataCard>

      {/* Active sessions */}
      {runningSessions.length > 0 && (
        <DataCard title="Running Sessions" description="Currently active trading sessions">
          <div className="space-y-2">
            {runningSessions.map((session) => (
              <div
                key={session.session_id}
                className="flex items-center justify-between rounded-lg border border-border-default bg-bg-secondary p-3 cursor-pointer hover:border-border-hover transition-colors"
                onClick={() => router.push(`/trading/${session.session_id}`)}
              >
                <div className="flex items-center gap-3">
                  <StatusBadge
                    status={session.trading_mode === 'paper' ? 'Paper' : 'Live'}
                    variant={session.trading_mode === 'paper' ? 'info' : 'warning'}
                    size="sm"
                  />
                  <div>
                    <p className="text-sm font-medium text-text-primary">{session.strategy_name || session.strategy_id}</p>
                    <p className="text-xs text-text-muted">
                      {session.symbols.join(', ')} &middot; {session.timeframe}
                      {session.started_at && ` &middot; Started ${new Date(session.started_at).toLocaleTimeString()}`}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <StatusBadge status="Running" variant="success" size="sm" />
                  <Eye className="h-4 w-4 text-text-muted" />
                </div>
              </div>
            ))}
          </div>
        </DataCard>
      )}

      {/* Quick Trade */}
      <DataCard title="Quick Trade">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Input
            label="Symbol"
            value={qtSymbol}
            onChange={(e) => setQtSymbol(e.target.value)}
            placeholder="BTCUSDT"
          />
          <div className="flex flex-col gap-1.5">
            <span className="text-sm font-medium text-text-secondary">Side</span>
            <div className="flex gap-2">
              <Button
                variant={qtSide === 'buy' ? 'primary' : 'outline'}
                size="sm"
                className={qtSide === 'buy' ? 'bg-status-success hover:bg-status-success/90' : ''}
                onClick={() => setQtSide('buy')}
              >
                Buy
              </Button>
              <Button
                variant={qtSide === 'sell' ? 'danger' : 'outline'}
                size="sm"
                onClick={() => setQtSide('sell')}
              >
                Sell
              </Button>
            </div>
          </div>
          <Input
            label="Quantity"
            type="number"
            value={qtQuantity}
            onChange={(e) => setQtQuantity(e.target.value)}
            placeholder="0.001"
          />
          <Select
            label="Order Type"
            value={qtOrderType}
            onChange={(e) => setQtOrderType(e.target.value)}
            options={[
              { value: 'market', label: 'Market' },
              { value: 'limit', label: 'Limit' },
            ]}
          />
          {qtOrderType === 'limit' && (
            <Input
              label="Price"
              type="number"
              value={qtPrice}
              onChange={(e) => setQtPrice(e.target.value)}
              placeholder="65000"
            />
          )}
          <Input
            label="Take Profit"
            type="number"
            value={qtTakeProfit}
            onChange={(e) => setQtTakeProfit(e.target.value)}
            placeholder="Optional"
          />
          <Input
            label="Stop Loss"
            type="number"
            value={qtStopLoss}
            onChange={(e) => setQtStopLoss(e.target.value)}
            placeholder="Optional"
          />
          <div className="flex items-end">
            <Button
              variant={qtSide === 'buy' ? 'primary' : 'danger'}
              fullWidth
              onClick={handleQuickTrade}
              loading={qtSubmitting}
            >
              <Zap className="h-4 w-4" />
              {qtSide === 'buy' ? 'Buy' : 'Sell'} {qtSymbol}
            </Button>
          </div>
        </div>
      </DataCard>

      {/* Order Book */}
      {fetchErrors.orderBook ? (
        <ErrorCard message="Failed to load order book" onRetry={loadData} />
      ) : orderBook ? (
        <DataCard title="Order Book">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-status-success">Bids</p>
              <div className="space-y-1">
                <div className="flex justify-between text-xs text-text-muted border-b border-border-default pb-1">
                  <span>Price</span>
                  <span>Qty</span>
                </div>
                {orderBook.bids.slice(0, 10).map(([price, qty], i) => (
                  <div key={i} className="flex justify-between text-xs">
                    <span className="text-status-success font-mono">${price.toLocaleString('en-US', { minimumFractionDigits: 2 })}</span>
                    <span className="text-text-secondary font-mono">{qty.toFixed(4)}</span>
                  </div>
                ))}
              </div>
            </div>
            <div>
              <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-status-error">Asks</p>
              <div className="space-y-1">
                <div className="flex justify-between text-xs text-text-muted border-b border-border-default pb-1">
                  <span>Price</span>
                  <span>Qty</span>
                </div>
                {orderBook.asks.slice(0, 10).map(([price, qty], i) => (
                  <div key={i} className="flex justify-between text-xs">
                    <span className="text-status-error font-mono">${price.toLocaleString('en-US', { minimumFractionDigits: 2 })}</span>
                    <span className="text-text-secondary font-mono">{qty.toFixed(4)}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </DataCard>
      ) : null}

      {/* Candlestick chart */}
      <DataCard title="BTC/USDT" description="Live price chart">
        <div className="tradingview-widget-container h-[28rem] md:h-[36rem] w-full" ref={widgetContainerRef}>
          <div className="tradingview-widget-container__widget h-full w-full" />
        </div>
      </DataCard>

      {/* Open positions */}
      <DataCard title="Open Positions" description="Currently active positions">
        {fetchErrors.positions ? (
          <ErrorCard message="Failed to load positions" onRetry={loadData} />
        ) : loading ? (
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-12 animate-pulse rounded-lg bg-bg-tertiary" />
            ))}
          </div>
        ) : openPositions && openPositions.length > 0 ? (
          <Table
            columns={positionColumns}
            data={openPositions}
            keyExtractor={(row) => row.id}
          />
        ) : (
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>

      {/* Recent trades */}
      <DataCard title="Recent Trades">
        {fetchErrors.trades ? (
          <ErrorCard message="Failed to load recent trades" onRetry={loadData} />
        ) : loading ? (
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-12 animate-pulse rounded-lg bg-bg-tertiary" />
            ))}
          </div>
        ) : recentTrades && recentTrades.length > 0 ? (
          <>
            <div className="flex items-center gap-2 mb-3">
              <ArrowUpDown className="h-4 w-4 text-text-muted" />
              <span className="text-xs text-text-muted">Sorted by most recent</span>
            </div>
            <Table
              columns={tradeHistoryColumns}
              data={recentTrades}
              keyExtractor={(row) => row.id}
            />
          </>
        ) : (
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>
    </div>
  );
}
