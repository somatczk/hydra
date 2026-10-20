'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { Bell, Cog, CheckCircle, XCircle, OctagonX, ChevronDown, ChevronUp, Copy, Link } from 'lucide-react';
import { DataCard } from '@/components/ui/DataCard';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { cn } from '@/components/ui/cn';
import { fetchApi } from '@/lib/api';
import { describeApiError } from '@/lib/apiState';
import { CLEAR_WEBHOOK_SECRET_WARNING, TRADING_MODE_OPTIONS } from '@/lib/platformConfig';
import { useToast } from '@/components/ui/Toast';
import { logger } from '@/lib/logger';
import { ExchangeConnectDialog } from '@/components/settings/ExchangeConnectDialog';

/* ---------- Types ---------- */

type CredentialState = 'ok' | 'undecryptable' | 'missing';
type Environment = 'testnet' | 'mainnet';

interface Exchange {
  id: string;
  name: string;
  logo: string;
  supported: boolean;
  connected: boolean;
  apiKeySet: boolean;
  credentialState: CredentialState;
  validatedAt: string | null;
  environment: Environment;
  validatedEnvironment: Environment | null;
}

interface ApiExchange {
  id: string;
  name: string;
  supported: boolean;
  connected: boolean;
  api_key_set: boolean;
  credential_state: CredentialState;
  last_sync: string | null;
  environment: Environment;
  validated_environment: Environment | null;
}

interface PlatformConfig {
  trading_mode: string;
  default_pair: string;
  default_timeframe: string;
  max_concurrent_strategies: number;
  paper_capital: number;
  paper_capital_pending: number | null;
  webhook_secret_set: boolean;
  discord_webhook_url_set: boolean;
  slack_webhook_url_set: boolean;
}

type SecretField = 'webhook_secret' | 'discord_webhook_url' | 'slack_webhook_url';
type SecretsSet = Record<SecretField, boolean>;

const SECRET_PLACEHOLDER = 'Configured, enter a new value to replace';

interface RiskConfig {
  kill_switch_active: boolean;
}

interface ExchangeBalance {
  currency: string;
  available: number;
  total: number;
}

interface ApiOrder {
  symbol: string;
  side: string;
  type: string | null;
  amount: number | null;
  price: number | null;
  status: string | null;
}

interface ExchangeOrder {
  symbol: string;
  side: string;
  type: string;
  quantity: number | null;
  price: number | null;
  status: string;
}

type ConnectionStatus = 'live' | 'error' | 'retrying';

interface Panel<T> {
  data: T | null;
  error: string | null;
}

interface ExchangeDetail {
  balances: Panel<ExchangeBalance[]>;
  orders: Panel<ExchangeOrder[]>;
  status: ConnectionStatus;
  error: string | null;
}

/* ---------- Helpers ---------- */

const logoMap: Record<string, string> = {
  binance: 'B',
  bybit: 'BY',
  kraken: 'K',
  okx: 'O',
  coinbase: 'CB',
  kucoin: 'KU',
  gateio: 'G',
  mexc: 'MX',
  bitget: 'BG',
};

function mapApiExchanges(data: ApiExchange[]): Exchange[] {
  return data.map((e) => ({
    id: e.id,
    name: e.name,
    logo: logoMap[e.id] || e.name[0],
    supported: e.supported,
    connected: e.connected,
    apiKeySet: e.api_key_set,
    credentialState: e.credential_state,
    validatedAt: e.last_sync,
    environment: e.environment,
    validatedEnvironment: e.validated_environment,
  }));
}

function validationText(exchange: Exchange): string {
  if (!exchange.supported) return 'Not supported yet (Binance only)';
  if (exchange.credentialState === 'undecryptable') return 'Stored keys cannot be decrypted';
  if (!exchange.validatedAt) return exchange.apiKeySet ? 'Never validated' : 'Not connected';
  if (exchange.validatedEnvironment !== exchange.environment) {
    return `Checked on ${exchange.validatedEnvironment ?? 'an unknown environment'} only; connect again for ${exchange.environment}`;
  }
  return `Validated on ${exchange.environment} ${new Date(exchange.validatedAt).toLocaleString()}`;
}

function toOrder(o: ApiOrder): ExchangeOrder {
  return {
    symbol: o.symbol,
    side: o.side,
    type: o.type ?? '',
    quantity: o.amount,
    price: o.price,
    status: o.status ?? '',
  };
}

const CREDENTIAL_LABELS: Record<CredentialState, string> = {
  ok: 'Stored (encrypted)',
  undecryptable: 'Undecryptable, enter the keys again',
  missing: 'Not set',
};

const BACKOFF_STEPS = [30000, 60000, 120000, 300000];

/* ---------- Page ---------- */

export default function SettingsPage() {
  const { toast } = useToast();
  const [exchanges, setExchanges] = useState<Exchange[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchErrors, setFetchErrors] = useState<Record<string, unknown>>({});
  const [tradingMode, setTradingMode] = useState('paper');
  const [defaultPair, setDefaultPair] = useState('BTC/USDT');
  const [defaultTimeframe, setDefaultTimeframe] = useState('1h');
  const [maxStrategies, setMaxStrategies] = useState('5');
  const [paperCapital, setPaperCapital] = useState('');
  const [savedPaperCapital, setSavedPaperCapital] = useState<number | null>(null);
  const [pendingPaperCapital, setPendingPaperCapital] = useState<number | null>(null);
  // null = unknown: the risk config could not be read
  const [killSwitchActive, setKillSwitchActive] = useState<boolean | null>(null);
  const [notifications, setNotifications] = useState<Record<string, boolean> | null>(null);
  const [notificationsError, setNotificationsError] = useState<unknown>(null);
  const [secretsSet, setSecretsSet] = useState<SecretsSet>({
    webhook_secret: false,
    discord_webhook_url: false,
    slack_webhook_url: false,
  });
  const [selectedExchange, setSelectedExchange] = useState<Exchange | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [webhookSecret, setWebhookSecret] = useState('');
  const [discordWebhookUrl, setDiscordWebhookUrl] = useState('');
  const [slackWebhookUrl, setSlackWebhookUrl] = useState('');
  const [savingIntegrations, setSavingIntegrations] = useState(false);

  // Exchange detail panels
  const [expandedExchange, setExpandedExchange] = useState<string | null>(null);
  const [exchangeDetails, setExchangeDetails] = useState<Record<string, ExchangeDetail>>({});
  const pollTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const failCounts = useRef<Record<string, number>>({});

  const applySecretsSet = (cfg: PlatformConfig) => {
    setSecretsSet({
      webhook_secret: cfg.webhook_secret_set,
      discord_webhook_url: cfg.discord_webhook_url_set,
      slack_webhook_url: cfg.slack_webhook_url_set,
    });
  };

  // Secrets are write-only: only fields in `patch` are sent, '' clears one.
  const saveSecrets = async (patch: Partial<Record<SecretField, string>>, success: string) => {
    const cfg = await fetchApi<PlatformConfig>('/api/system/config', {
      method: 'PUT',
      body: JSON.stringify(patch),
    });
    applySecretsSet(cfg);
    toast('success', success);
  };

  const loadData = useCallback(async () => {
    setLoading(true);
    setFetchErrors({});

    await Promise.all([
      fetchApi<ApiExchange[]>('/api/system/exchanges')
        .then((ex) => setExchanges(mapApiExchanges(ex)))
        .catch((err) => { setFetchErrors((prev) => ({ ...prev, exchanges: err ?? true })); }),
      fetchApi<PlatformConfig>('/api/system/config')
        .then((cfg) => {
          setTradingMode(cfg.trading_mode);
          setDefaultPair(cfg.default_pair);
          setDefaultTimeframe(cfg.default_timeframe);
          setMaxStrategies(cfg.max_concurrent_strategies.toString());
          setPaperCapital(cfg.paper_capital.toString());
          setSavedPaperCapital(cfg.paper_capital);
          setPendingPaperCapital(cfg.paper_capital_pending);
          applySecretsSet(cfg);
        })
        .catch((err) => { setFetchErrors((prev) => ({ ...prev, config: err ?? true })); }),
      fetchApi<RiskConfig>('/api/risk/config')
        .then((risk) => setKillSwitchActive(risk.kill_switch_active))
        .catch((err) => {
          logger.warn('Settings', 'Kill switch state unknown', err);
          setKillSwitchActive(null);
        }),
      fetchApi<{ preferences: Record<string, boolean> }>('/api/system/notifications')
        .then((data) => { setNotifications(data.preferences); setNotificationsError(null); })
        .catch((err) => {
          logger.warn('Settings', 'Failed to load notification preferences', err);
          setNotifications(null);
          setNotificationsError(err);
        }),
    ]);

    setLoading(false);
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  // Poll kill switch state every 15s so banner stays in sync
  useEffect(() => {
    const pollKillSwitch = () => {
      fetchApi<{ kill_switch_active: boolean | null }>('/api/risk/live-status')
        .then((data) => setKillSwitchActive(data.kill_switch_active))
        .catch(() => setKillSwitchActive(null));
    };
    const interval = setInterval(pollKillSwitch, 15_000);
    return () => clearInterval(interval);
  }, []);

  // Each panel loads on its own: one failing read never hides the other
  const fetchExchangeDetail = useCallback((exchangeId: string) => {
    async function load(): Promise<void> {
      const [balancesRes, ordersRes] = await Promise.allSettled([
        fetchApi<{ exchange_id: string; balances: Record<string, number> }>(`/api/system/exchanges/${exchangeId}/balance`),
        fetchApi<{ exchange_id: string; orders: ApiOrder[] }>(`/api/system/exchanges/${exchangeId}/orders`),
      ]);
      const balances: Panel<ExchangeBalance[]> = balancesRes.status === 'fulfilled'
        ? {
            data: Object.entries(balancesRes.value.balances || {}).map(
              ([currency, amount]) => ({ currency, available: amount, total: amount }),
            ),
            error: null,
          }
        : { data: null, error: describeApiError(balancesRes.reason, 'Balance unavailable') };
      const orders: Panel<ExchangeOrder[]> = ordersRes.status === 'fulfilled'
        ? { data: (ordersRes.value.orders || []).map(toOrder), error: null }
        : { data: null, error: describeApiError(ordersRes.reason, 'Open orders unavailable') };
      const failed = [balances.error, orders.error].filter((e): e is string => e !== null);

      setExchangeDetails((prev) => ({
        ...prev,
        [exchangeId]: {
          balances,
          orders,
          status: failed.length === 0 ? 'live' : 'retrying',
          error: failed[0] ?? null,
        },
      }));
      if (failed.length === 0) {
        failCounts.current[exchangeId] = 0;
        pollTimers.current[exchangeId] = setTimeout(load, 30000);
        return;
      }
      const count = (failCounts.current[exchangeId] || 0) + 1;
      failCounts.current[exchangeId] = count;
      const backoff = BACKOFF_STEPS[Math.min(count - 1, BACKOFF_STEPS.length - 1)];
      pollTimers.current[exchangeId] = setTimeout(load, backoff);
    }
    void load();
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
    const capital = Number(paperCapital);
    if (
      savedPaperCapital !== null &&
      capital !== savedPaperCapital &&
      !confirm(
        `Change paper capital from ${savedPaperCapital} to ${capital} USDT?\n\n` +
          'The paper account ledger is reset to the new capital (audited): peak, day start and ' +
          'carried PnL are rebased. Circuit breaker tiers are not cleared.',
      )
    ) {
      return;
    }
    try {
      const cfg = await fetchApi<PlatformConfig>('/api/system/config', {
        method: 'PUT',
        body: JSON.stringify({
          trading_mode: tradingMode,
          default_pair: defaultPair,
          default_timeframe: defaultTimeframe,
          max_concurrent_strategies: Number(maxStrategies),
          paper_capital: capital,
        }),
      });
      setSavedPaperCapital(cfg.paper_capital);
      setPendingPaperCapital(cfg.paper_capital_pending);
      if (typeof cfg.paper_capital_pending === 'number') {
        toast('warning', 'Saved. The paper ledger switches to the new capital once every held position has a price');
      } else {
        toast('success', 'Platform configuration saved');
      }
    } catch (err) {
      logger.error('Settings', 'Failed to save config', err);
      toast('error', describeApiError(err, 'Failed to save configuration'));
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
      {!loading && killSwitchActive === null && (
        <div className="rounded-xl border border-status-warning/30 bg-status-warning/5 p-4 flex items-center gap-3">
          <DataStateBadge state="unknown" reason="Risk config could not be read" />
          <p className="text-sm text-text-primary">
            Kill switch state is unknown. Check the Risk page before trading.
          </p>
        </div>
      )}
      {killSwitchActive === true && (
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
          <DataStateErrorCard error={fetchErrors.exchanges} fallback="Failed to load exchange connections" onRetry={loadData} />
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
                          <p className="mt-0.5 text-xs text-text-muted" data-testid={`validation-${exchange.id}`}>
                            {validationText(exchange)}
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
                      <span className="flex items-center gap-2 text-xs text-text-muted">
                        API Key: {CREDENTIAL_LABELS[exchange.credentialState]}
                        {exchange.credentialState === 'undecryptable' && (
                          <DataStateBadge state="invalid" reason="HYDRA_CREDENTIAL_KEY cannot decrypt the stored keys" />
                        )}
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
                          disabled={!exchange.supported}
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
                        {detail?.balances.error ? (
                          <p className="text-xs text-status-error" data-testid={`balances-error-${exchange.id}`}>{detail.balances.error}</p>
                        ) : detail?.balances.data && detail.balances.data.length > 0 ? (
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
                                {detail.balances.data.map((b) => (
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
                        {detail?.orders.error ? (
                          <p className="text-xs text-status-error" data-testid={`orders-error-${exchange.id}`}>{detail.orders.error}</p>
                        ) : detail?.orders.data && detail.orders.data.length > 0 ? (
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
                                {detail.orders.data.map((o, i) => (
                                  <tr key={i}>
                                    <td className="px-2 py-1.5 text-text-primary">{o.symbol}</td>
                                    <td className="px-2 py-1.5">
                                      <StatusBadge status={o.side} variant={o.side.toLowerCase() === 'buy' ? 'success' : 'error'} size="sm" />
                                    </td>
                                    <td className="px-2 py-1.5 text-text-primary">{o.type}</td>
                                    <td className="px-2 py-1.5 text-right text-text-primary">{o.quantity ?? '-'}</td>
                                    <td className="px-2 py-1.5 text-right text-text-primary">{o.price === null ? 'Market' : `$${o.price.toLocaleString('en-US', { minimumFractionDigits: 2 })}`}</td>
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

                      {/* Positions: a spot account has none */}
                      <div>
                        <h5 className="text-xs font-semibold uppercase tracking-wider text-text-muted mb-2">Positions</h5>
                        <p className="text-xs text-text-muted">
                          Spot account: there are no positions; holdings are listed under Balances.
                        </p>
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
        {notificationsError !== null ? (
          <DataStateErrorCard
            error={notificationsError}
            fallback="Failed to load notification preferences"
            onRetry={loadData}
          />
        ) : notifications === null && loading ? (
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

      {/* Webhook Configuration */}
      <DataCard title="Webhook Configuration" description="Configure webhook endpoint for external signal ingestion">
        <div className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-text-secondary mb-1">Webhook URL</label>
            <div className="flex items-center gap-2">
              <div className="flex-1 rounded-lg border border-border-default bg-bg-tertiary px-3 py-2 text-sm text-text-muted font-mono">
                {typeof window !== 'undefined' ? `${window.location.origin}/api/signals/webhook` : '/api/signals/webhook'}
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  const url = `${window.location.origin}/api/signals/webhook`;
                  navigator.clipboard.writeText(url);
                  toast('success', 'Webhook URL copied to clipboard');
                }}
              >
                <Copy className="h-4 w-4" />
                Copy URL
              </Button>
            </div>
          </div>
          <Input
            label="Webhook Secret"
            type="password"
            autoComplete="off"
            value={webhookSecret}
            placeholder={secretsSet.webhook_secret ? SECRET_PLACEHOLDER : 'Not set'}
            onChange={(e) => setWebhookSecret(e.target.value)}
            hint="Shared secret for verifying webhook payloads"
          />
          <div className="flex justify-end gap-2">
            {secretsSet.webhook_secret && (
              <Button
                variant="outline"
                size="sm"
                onClick={async () => {
                  if (!confirm(CLEAR_WEBHOOK_SECRET_WARNING)) return;
                  try {
                    await saveSecrets({ webhook_secret: '' }, 'Webhook secret cleared');
                  } catch (err) {
                    toast('error', describeApiError(err, 'Failed to clear webhook secret'));
                  }
                }}
              >
                Clear
              </Button>
            )}
            <Button
              variant="primary"
              size="sm"
              disabled={webhookSecret === ''}
              onClick={async () => {
                try {
                  await saveSecrets({ webhook_secret: webhookSecret }, 'Webhook secret saved');
                  setWebhookSecret('');
                } catch (err) {
                  toast('error', describeApiError(err, 'Failed to save webhook secret'));
                }
              }}
            >
              Save Secret
            </Button>
          </div>
        </div>
      </DataCard>

      {/* Integration URLs */}
      <DataCard title="Integration URLs" description="Connect Discord and Slack for notifications">
        <div className="space-y-4">
          {([
            ['discord_webhook_url', 'Discord Webhook URL', discordWebhookUrl, setDiscordWebhookUrl, 'https://discord.com/api/webhooks/...'],
            ['slack_webhook_url', 'Slack Webhook URL', slackWebhookUrl, setSlackWebhookUrl, 'https://hooks.slack.com/services/...'],
          ] as const).map(([field, label, value, setValue, example]) => (
            <div key={field} className="flex items-end gap-2">
              <div className="flex-1">
                <Input
                  label={label}
                  type="text"
                  autoComplete="off"
                  value={value}
                  placeholder={secretsSet[field] ? SECRET_PLACEHOLDER : example}
                  onChange={(e) => setValue(e.target.value)}
                  hint={`${label.replace(' URL', '')} for trade notifications`}
                />
              </div>
              {secretsSet[field] && (
                <Button
                  variant="outline"
                  size="sm"
                  className="mb-6"
                  onClick={async () => {
                    try {
                      await saveSecrets({ [field]: '' }, `${label} cleared`);
                    } catch (err) {
                      toast('error', describeApiError(err, `Failed to clear ${label}`));
                    }
                  }}
                >
                  Clear
                </Button>
              )}
            </div>
          ))}
          <div className="flex justify-end">
            <Button
              variant="primary"
              size="sm"
              loading={savingIntegrations}
              disabled={discordWebhookUrl === '' && slackWebhookUrl === ''}
              onClick={async () => {
                const patch: Partial<Record<SecretField, string>> = {};
                if (discordWebhookUrl !== '') patch.discord_webhook_url = discordWebhookUrl;
                if (slackWebhookUrl !== '') patch.slack_webhook_url = slackWebhookUrl;
                setSavingIntegrations(true);
                try {
                  await saveSecrets(patch, 'Integration URLs saved');
                  setDiscordWebhookUrl('');
                  setSlackWebhookUrl('');
                } catch (err) {
                  toast('error', describeApiError(err, 'Failed to save integration URLs'));
                }
                setSavingIntegrations(false);
              }}
            >
              <Link className="h-4 w-4" />
              Save Integrations
            </Button>
          </div>
        </div>
      </DataCard>

      {/* Platform config */}
      <DataCard title="Platform Configuration" description="General platform settings">
        {fetchErrors.config ? (
          <DataStateErrorCard error={fetchErrors.config} fallback="Failed to load platform configuration" onRetry={loadData} />
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
                  { value: 'BTC/USDT', label: 'BTC/USDT' },
                  { value: 'ETH/USDT', label: 'ETH/USDT' },
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
                options={[...TRADING_MODE_OPTIONS]}
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
                hint={
                  pendingPaperCapital !== null
                    ? `Pending: the paper ledger moves to ${pendingPaperCapital} USDT once every held position has a price`
                    : 'Capital of the paper account; changing it resets the paper ledger (audited)'
                }
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
