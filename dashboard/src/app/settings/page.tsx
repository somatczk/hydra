'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { Bell, Cog, CheckCircle, XCircle, OctagonX, ChevronDown, ChevronUp } from 'lucide-react';
import { DataCard } from '@/components/ui/DataCard';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { ErrorCard } from '@/components/ui/ErrorCard';
import { cn } from '@/components/ui/cn';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { logger } from '@/lib/logger';
import { ExchangeConnectDialog } from '@/components/settings/ExchangeConnectDialog';

/* ---------- Types ---------- */

interface Exchange {
  id: string;
  name: string;
  logo: string;
  connected: boolean;
  apiKeySet: boolean;
  lastSync: string;
}

interface ApiExchange {
  id: string;
  name: string;
  connected: boolean;
  api_key_set: boolean;
  last_sync: string;
}

interface PlatformConfig {
  trading_mode: string;
  default_pair: string;
  default_timeframe: string;
  max_concurrent_strategies: number;
  paper_capital: number;
}

interface RiskConfig {
  kill_switch_active: boolean;
}

interface ExchangeBalance {
  currency: string;
  available: number;
  total: number;
}

interface ExchangeOrder {
  symbol: string;
  side: string;
  type: string;
  quantity: number;
  price: number;
  status: string;
}

interface ExchangePosition {
  symbol: string;
  direction: string;
  quantity: number;
  entry: number;
  unrealized_pnl: number;
}

type ConnectionStatus = 'live' | 'error' | 'retrying';

interface ExchangeDetail {
  balances: ExchangeBalance[] | null;
  orders: ExchangeOrder[] | null;
  positions: ExchangePosition[] | null;
  status: ConnectionStatus;
  error: string | null;
}

/* ---------- Helpers ---------- */

const logoMap: Record<string, string> = {
  binance: 'B',
  bybit: 'BY',
  kraken: 'K',
  okx: 'O',
};

function mapApiExchanges(data: ApiExchange[]): Exchange[] {
  return data.map((e) => ({
    id: e.id,
    name: e.name,
    logo: logoMap[e.id] || e.name[0],
    connected: e.connected,
    apiKeySet: e.api_key_set,
    lastSync: e.last_sync,
  }));
}

const BACKOFF_STEPS = [30000, 60000, 120000, 300000];

/* ---------- Page ---------- */

export default function SettingsPage() {
  const { toast } = useToast();
  const [exchanges, setExchanges] = useState<Exchange[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchErrors, setFetchErrors] = useState<Record<string, boolean>>({});
  const [tradingMode, setTradingMode] = useState('paper');
  const [defaultPair, setDefaultPair] = useState('btcusdt');
  const [defaultTimeframe, setDefaultTimeframe] = useState('1h');
  const [maxStrategies, setMaxStrategies] = useState('5');
  const [paperCapital, setPaperCapital] = useState('10000');
  const [killSwitchActive, setKillSwitchActive] = useState(false);
  const [notifications, setNotifications] = useState<Record<string, boolean> | null>(null);
  const [selectedExchange, setSelectedExchange] = useState<Exchange | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  // Exchange detail panels
  const [expandedExchange, setExpandedExchange] = useState<string | null>(null);
  const [exchangeDetails, setExchangeDetails] = useState<Record<string, ExchangeDetail>>({});
  const pollTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const failCounts = useRef<Record<string, number>>({});

  const loadData = useCallback(async () => {
    setLoading(true);
    setFetchErrors({});

    await Promise.all([
      fetchApi<ApiExchange[]>('/api/system/exchanges')
        .then((ex) => setExchanges(mapApiExchanges(ex)))
        .catch(() => { setFetchErrors((prev) => ({ ...prev, exchanges: true })); }),
      fetchApi<PlatformConfig>('/api/system/config')
        .then((cfg) => {
          setTradingMode(cfg.trading_mode);
          setDefaultPair(cfg.default_pair);
          setDefaultTimeframe(cfg.default_timeframe);
          setMaxStrategies(cfg.max_concurrent_strategies.toString());
          if (cfg.paper_capital) setPaperCapital(cfg.paper_capital.toString());
        })
        .catch(() => { setFetchErrors((prev) => ({ ...prev, config: true })); }),
      fetchApi<RiskConfig>('/api/risk/config')
        .then((risk) => setKillSwitchActive(risk.kill_switch_active))
        .catch(() => {}),
      fetchApi<{ preferences: Record<string, boolean> }>('/api/system/notifications')
        .then((data) => setNotifications(data.preferences || data as unknown as Record<string, boolean>))
        .catch(() => {
          // Fallback to defaults if API fails
          setNotifications({
            'Trade Executions': true,
            'Risk Alerts': true,
            'Model Drift': true,
            'Strategy Signals': false,
            'System Health': true,
          });
        }),
    ]);

    setLoading(false);
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  // Poll kill switch state every 15s so banner stays in sync
  useEffect(() => {
    const pollKillSwitch = () => {
      fetchApi<{ kill_switch_active: boolean; running_sessions: number }>('/api/risk/live-status')
        .then((data) => setKillSwitchActive(data.kill_switch_active))
        .catch(() => {});
    };
    const interval = setInterval(pollKillSwitch, 15_000);
    return () => clearInterval(interval);
  }, []);

  // Fetch exchange detail data with polling
  const fetchExchangeDetail = useCallback(async (exchangeId: string) => {
    try {
      const [balancesRaw, ordersRaw, positionsRaw] = await Promise.all([
        fetchApi<{ exchange_id: string; balances: Record<string, number> }>(`/api/system/exchanges/${exchangeId}/balance`),
        fetchApi<{ exchange_id: string; orders: ExchangeOrder[] }>(`/api/system/exchanges/${exchangeId}/orders`),
        fetchApi<{ exchange_id: string; positions: ExchangePosition[] }>(`/api/system/exchanges/${exchangeId}/positions`),
      ]);
      const balances: ExchangeBalance[] = Object.entries(balancesRaw.balances || {}).map(
        ([currency, amount]) => ({ currency, available: amount, total: amount })
      );
      const orders = ordersRaw.orders || [];
      const positions = positionsRaw.positions || [];
      setExchangeDetails((prev) => ({
        ...prev,
        [exchangeId]: { balances, orders, positions, status: 'live', error: null },
      }));
      failCounts.current[exchangeId] = 0;
      // Success: poll again in 30s
      pollTimers.current[exchangeId] = setTimeout(() => fetchExchangeDetail(exchangeId), 30000);
    } catch (err) {
      const count = (failCounts.current[exchangeId] || 0) + 1;
      failCounts.current[exchangeId] = count;
      const backoffIdx = Math.min(count - 1, BACKOFF_STEPS.length - 1);
      const backoff = BACKOFF_STEPS[backoffIdx];

      setExchangeDetails((prev) => ({
        ...prev,
        [exchangeId]: {
          ...(prev[exchangeId] || { balances: null, orders: null, positions: null }),
          status: 'retrying',
          error: err instanceof Error ? err.message : 'Connection failed',
        },
      }));

      pollTimers.current[exchangeId] = setTimeout(() => {
        setExchangeDetails((prev) => ({
          ...prev,
          [exchangeId]: { ...(prev[exchangeId] || { balances: null, orders: null, positions: null }), status: 'retrying', error: prev[exchangeId]?.error || null },
        }));
        fetchExchangeDetail(exchangeId);
      }, backoff);
    }
  }, []);

  const toggleExchangePanel = useCallback((exchangeId: string) => {
    if (expandedExchange === exchangeId) {
      setExpandedExchange(null);
      // Clear polling timer
      if (pollTimers.current[exchangeId]) {
        clearTimeout(pollTimers.current[exchangeId]);
        delete pollTimers.current[exchangeId];
      }
    } else {
      setExpandedExchange(exchangeId);
      fetchExchangeDetail(exchangeId);
    }
  }, [expandedExchange, fetchExchangeDetail]);

  // Cleanup polling timers on unmount
  useEffect(() => {
    return () => {
      Object.values(pollTimers.current).forEach(clearTimeout);
    };
  }, []);

  const refreshExchanges = async () => {
    try {
      const ex = await fetchApi<ApiExchange[]>('/api/system/exchanges');
      setExchanges(mapApiExchanges(ex));
    } catch (err) { logger.warn('Settings', 'Failed to refresh exchanges', err); }
  };

  const handleSaveConfig = async () => {
    try {
      await fetchApi('/api/system/config', {
        method: 'PUT',
        body: JSON.stringify({
          trading_mode: tradingMode,
          default_pair: defaultPair,
          default_timeframe: defaultTimeframe,
          max_concurrent_strategies: Number(maxStrategies),
          paper_capital: Number(paperCapital),
        }),
      });
      toast('success', 'Platform configuration saved');
    } catch (err) {
      logger.error('Settings', 'Failed to save config', err);
      toast('error', 'Failed to save configuration');
    }
  };

  const handleNotificationToggle = async (label: string) => {
    const updated = { ...notifications, [label]: !notifications?.[label] };
    setNotifications(updated);
    try {
      await fetchApi('/api/system/notifications', {
        method: 'PUT',
        body: JSON.stringify({ preferences: updated }),
      });
    } catch (err) {
      logger.error('Settings', 'Failed to save notification preferences', err);
      toast('error', 'Failed to save notification preferences');
      // Revert on error
      setNotifications(notifications);
    }
  };

  const connectionStatusBadge = (detail: ExchangeDetail | undefined) => {
    if (!detail) return null;
    switch (detail.status) {
      case 'live':
        return <StatusBadge status="Live" variant="success" size="sm" />;
      case 'error':
        return <StatusBadge status={`Error: ${detail.error || 'Unknown'}`} variant="error" size="sm" />;
      case 'retrying':
        return <StatusBadge status="Retrying..." variant="warning" size="sm" />;
    }
  };

  return (
    <div className="flex flex-col gap-6">
      {/* Kill switch warning banner */}
      {killSwitchActive && (
        <div className="rounded-xl border-2 border-status-error bg-status-error/10 p-4 flex items-center gap-3">
          <OctagonX className="h-6 w-6 text-status-error shrink-0" />
          <div>
            <p className="text-sm font-semibold text-status-error">Kill Switch Active</p>
            <p className="text-xs text-text-muted">
              All trading is halted. Release the kill switch from the Risk page to resume trading.
            </p>
          </div>
        </div>
      )}

      {/* Exchange connections */}
      <DataCard title="Exchange Connections" description="Manage your exchange API connections">
        {fetchErrors.exchanges ? (
          <ErrorCard message="Failed to load exchange connections" onRetry={loadData} />
        ) : loading ? (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="h-28 animate-pulse rounded-xl border border-border-default bg-bg-tertiary" />
            ))}
          </div>
        ) : exchanges && exchanges.length > 0 ? (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {exchanges.map((exchange) => {
              const detail = exchangeDetails[exchange.id];
              const isExpanded = expandedExchange === exchange.id;
              return (
                <div key={exchange.id} className="flex flex-col">
                  <div
                    className={cn(
                      'rounded-xl border p-4 transition-colors',
                      exchange.connected
                        ? 'border-status-success/30 bg-status-success/5'
                        : 'border-border-default bg-bg-secondary',
                      isExpanded && 'rounded-b-none',
                    )}
                  >
                    <div className="flex items-start justify-between">
                      <div className="flex items-center gap-3">
                        <div
                          className={cn(
                            'flex h-10 w-10 items-center justify-center rounded-lg text-sm font-bold',
                            exchange.connected
                              ? 'bg-status-success/20 text-status-success'
                              : 'bg-bg-tertiary text-text-muted',
                          )}
                        >
                          {exchange.logo}
                        </div>
                        <div>
                          <h4 className="text-sm font-semibold text-text-primary">
                            {exchange.name}
                          </h4>
                          <p className="mt-0.5 text-xs text-text-muted">
                            Last sync: {exchange.lastSync}
                          </p>
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        {detail && connectionStatusBadge(detail)}
                        {exchange.connected ? (
                          <CheckCircle className="h-5 w-5 text-status-success" aria-label="Connected" />
                        ) : (
                          <XCircle className="h-5 w-5 text-text-light" aria-label="Disconnected" />
                        )}
                      </div>
                    </div>
                    <div className="mt-3 flex items-center justify-between">
                      <span className="text-xs text-text-muted">
                        API Key: {exchange.apiKeySet ? 'Configured' : 'Not set'}
                      </span>
                      <div className="flex items-center gap-2">
                        {exchange.connected && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => toggleExchangePanel(exchange.id)}
                          >
                            {isExpanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                            Details
                          </Button>
                        )}
                        <Button
                          variant={exchange.connected ? 'ghost' : 'outline'}
                          size="sm"
                          onClick={() => {
                            setSelectedExchange(exchange);
                            setModalOpen(true);
                          }}
                        >
                          {exchange.connected ? 'Manage' : 'Connect'}
                        </Button>
                      </div>
                    </div>
                  </div>

                  {/* Expandable detail panel */}
                  {isExpanded && exchange.connected && (
                    <div className="rounded-b-xl border border-t-0 border-border-default bg-bg-secondary p-4 space-y-4">
                      {/* Balances */}
                      <div>
                        <h5 className="text-xs font-semibold uppercase tracking-wider text-text-muted mb-2">Balances</h5>
                        {detail?.balances && detail.balances.length > 0 ? (
                          <div className="overflow-x-auto">
                            <table className="w-full text-xs">
                              <thead>
                                <tr className="border-b border-border-default">
                                  <th className="px-2 py-1.5 text-left text-text-muted font-semibold">Currency</th>
                                  <th className="px-2 py-1.5 text-right text-text-muted font-semibold">Available</th>
                                  <th className="px-2 py-1.5 text-right text-text-muted font-semibold">Total</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-border-default">
                                {detail.balances.map((b) => (
                                  <tr key={b.currency}>
                                    <td className="px-2 py-1.5 text-text-primary font-medium">{b.currency}</td>
                                    <td className="px-2 py-1.5 text-right text-text-primary">{b.available.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                                    <td className="px-2 py-1.5 text-right text-text-primary">{b.total.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        ) : (
                          <p className="text-xs text-text-muted">No balance data</p>
                        )}
                      </div>

                      {/* Open Orders */}
                      <div>
                        <h5 className="text-xs font-semibold uppercase tracking-wider text-text-muted mb-2">Open Orders</h5>
                        {detail?.orders && detail.orders.length > 0 ? (
                          <div className="overflow-x-auto">
                            <table className="w-full text-xs">
                              <thead>
                                <tr className="border-b border-border-default">
                                  <th className="px-2 py-1.5 text-left text-text-muted font-semibold">Symbol</th>
                                  <th className="px-2 py-1.5 text-left text-text-muted font-semibold">Side</th>
                                  <th className="px-2 py-1.5 text-left text-text-muted font-semibold">Type</th>
                                  <th className="px-2 py-1.5 text-right text-text-muted font-semibold">Qty</th>
                                  <th className="px-2 py-1.5 text-right text-text-muted font-semibold">Price</th>
                                  <th className="px-2 py-1.5 text-left text-text-muted font-semibold">Status</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-border-default">
                                {detail.orders.map((o, i) => (
                                  <tr key={i}>
                                    <td className="px-2 py-1.5 text-text-primary">{o.symbol}</td>
                                    <td className="px-2 py-1.5">
                                      <StatusBadge status={o.side} variant={o.side.toLowerCase() === 'buy' ? 'success' : 'error'} size="sm" />
                                    </td>
                                    <td className="px-2 py-1.5 text-text-primary">{o.type}</td>
                                    <td className="px-2 py-1.5 text-right text-text-primary">{o.quantity}</td>
                                    <td className="px-2 py-1.5 text-right text-text-primary">${o.price.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                                    <td className="px-2 py-1.5">
                                      <StatusBadge status={o.status} variant="neutral" size="sm" />
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        ) : (
                          <p className="text-xs text-text-muted">No open orders</p>
                        )}
                      </div>

                      {/* Positions */}
                      <div>
                        <h5 className="text-xs font-semibold uppercase tracking-wider text-text-muted mb-2">Positions</h5>
                        {detail?.positions && detail.positions.length > 0 ? (
                          <div className="overflow-x-auto">
                            <table className="w-full text-xs">
                              <thead>
                                <tr className="border-b border-border-default">
                                  <th className="px-2 py-1.5 text-left text-text-muted font-semibold">Symbol</th>
                                  <th className="px-2 py-1.5 text-left text-text-muted font-semibold">Direction</th>
                                  <th className="px-2 py-1.5 text-right text-text-muted font-semibold">Qty</th>
                                  <th className="px-2 py-1.5 text-right text-text-muted font-semibold">Entry</th>
                                  <th className="px-2 py-1.5 text-right text-text-muted font-semibold">Unrealized PnL</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-border-default">
                                {detail.positions.map((p, i) => (
                                  <tr key={i}>
                                    <td className="px-2 py-1.5 text-text-primary">{p.symbol}</td>
                                    <td className="px-2 py-1.5">
                                      <StatusBadge status={p.direction} variant={p.direction.toLowerCase() === 'long' ? 'success' : 'error'} size="sm" />
                                    </td>
                                    <td className="px-2 py-1.5 text-right text-text-primary">{p.quantity}</td>
                                    <td className="px-2 py-1.5 text-right text-text-primary">${p.entry.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                                    <td className={cn('px-2 py-1.5 text-right font-medium', p.unrealized_pnl >= 0 ? 'text-status-success' : 'text-status-error')}>
                                      {p.unrealized_pnl >= 0 ? '+' : ''}${p.unrealized_pnl.toFixed(2)}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        ) : (
                          <p className="text-xs text-text-muted">No open positions</p>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>

      {/* Notification preferences */}
      <DataCard title="Notification Preferences" description="Configure alert and notification settings">
        {notifications === null && loading ? (
          <div className="space-y-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-14 animate-pulse rounded-lg bg-bg-tertiary" />
            ))}
          </div>
        ) : notifications ? (
          <div className="space-y-4">
            {[
              { label: 'Trade Executions', description: 'Notify when trades are filled' },
              { label: 'Risk Alerts', description: 'Circuit breaker and drawdown warnings' },
              { label: 'Model Drift', description: 'Alert when model drift exceeds threshold' },
              { label: 'Strategy Signals', description: 'New signal generated by strategies' },
              { label: 'System Health', description: 'Service outages and connectivity issues' },
            ].map((pref) => {
              const enabled = notifications[pref.label] ?? false;
              return (
                <div
                  key={pref.label}
                  className="flex items-center justify-between rounded-lg border border-border-default bg-bg-secondary p-3"
                >
                  <div className="flex items-center gap-3">
                    <Bell className="h-4 w-4 text-text-muted shrink-0" aria-hidden="true" />
                    <div>
                      <p className="text-sm font-medium text-text-primary">{pref.label}</p>
                      <p className="text-xs text-text-muted">{pref.description}</p>
                    </div>
                  </div>
                  <div
                    className={cn(
                      'relative h-6 w-11 cursor-pointer rounded-full transition-colors',
                      enabled ? 'bg-accent-primary' : 'bg-bg-active',
                    )}
                    role="switch"
                    aria-checked={enabled}
                    aria-label={`${enabled ? 'Disable' : 'Enable'} ${pref.label}`}
                    tabIndex={0}
                    onClick={() => handleNotificationToggle(pref.label)}
                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleNotificationToggle(pref.label); } }}
                  >
                    <div
                      className={cn(
                        'absolute top-1 h-4 w-4 rounded-full bg-white shadow transition-transform',
                        enabled ? 'translate-x-[22px]' : 'translate-x-1',
                      )}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>

      {/* Platform config */}
      <DataCard title="Platform Configuration" description="General platform settings">
        {fetchErrors.config ? (
          <ErrorCard message="Failed to load platform configuration" onRetry={loadData} />
        ) : loading ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-16 animate-pulse rounded-lg bg-bg-tertiary" />
            ))}
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <Select
                label="Default Trading Pair"
                options={[
                  { value: 'btcusdt', label: 'BTC/USDT' },
                  { value: 'ethusdt', label: 'ETH/USDT' },
                  { value: 'btcusd', label: 'BTC/USD' },
                ]}
                value={defaultPair}
                onChange={(e) => setDefaultPair(e.target.value)}
              />
              <Select
                label="Default Timeframe"
                options={[
                  { value: '1m', label: '1 Minute' },
                  { value: '5m', label: '5 Minutes' },
                  { value: '15m', label: '15 Minutes' },
                  { value: '1h', label: '1 Hour' },
                  { value: '4h', label: '4 Hours' },
                ]}
                value={defaultTimeframe}
                onChange={(e) => setDefaultTimeframe(e.target.value)}
              />
              <Select
                label="Trading Mode"
                options={[
                  { value: 'paper', label: 'Paper Trading' },
                  { value: 'testnet', label: 'Testnet' },
                  { value: 'live', label: 'Live Trading' },
                ]}
                value={tradingMode}
                onChange={(e) => setTradingMode(e.target.value)}
                hint="Paper trading mode is recommended for testing"
              />
              <Input
                label="Max Concurrent Strategies"
                type="number"
                value={maxStrategies}
                onChange={(e) => setMaxStrategies(e.target.value)}
                hint="Maximum number of strategies running simultaneously"
              />
              <Input
                label="Paper Trading Capital (USDT)"
                type="number"
                value={paperCapital}
                onChange={(e) => setPaperCapital(e.target.value)}
                hint="Starting balance for paper trading sessions"
              />
            </div>
            <div className="mt-4 flex items-center justify-end gap-3">
              <Button variant="primary" onClick={handleSaveConfig}>
                <Cog className="h-4 w-4" />
                Save Configuration
              </Button>
            </div>
          </>
        )}
      </DataCard>
      <ExchangeConnectDialog
        open={modalOpen}
        onClose={() => { setModalOpen(false); setSelectedExchange(null); }}
        exchange={selectedExchange}
        onUpdated={refreshExchanges}
      />
    </div>
  );
}
