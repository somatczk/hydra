'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { Cog, CheckCircle, XCircle, ChevronDown, ChevronUp, Copy } from 'lucide-react';
import { Section } from '@/components/ui/Section';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { cn } from '@/components/ui/cn';
import { fetchApi } from '@/lib/api';
import { describeApiError } from '@/lib/apiState';
import { CLEAR_WEBHOOK_SECRET_WARNING } from '@/lib/platformConfig';
import { useTheme } from '@/components/layout/ThemeProvider';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
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
}

const SECRET_PLACEHOLDER = 'Configured, enter a new value to replace';

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
  const [tradingMode, setTradingMode] = useState<string | null>(null);
  const [defaultPair, setDefaultPair] = useState('BTC/USDT');
  const [defaultTimeframe, setDefaultTimeframe] = useState('1h');
  const [maxStrategies, setMaxStrategies] = useState('5');
  const [paperCapital, setPaperCapital] = useState('');
  const [savedPaperCapital, setSavedPaperCapital] = useState<number | null>(null);
  const [pendingPaperCapital, setPendingPaperCapital] = useState<number | null>(null);
  const [webhookSecretSet, setWebhookSecretSet] = useState(false);
  const [selectedExchange, setSelectedExchange] = useState<Exchange | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [webhookSecret, setWebhookSecret] = useState('');
  const [capitalConfirmOpen, setCapitalConfirmOpen] = useState(false);
  const { theme, setTheme } = useTheme();

  // Exchange detail panels
  const [expandedExchange, setExpandedExchange] = useState<string | null>(null);
  const [exchangeDetails, setExchangeDetails] = useState<Record<string, ExchangeDetail>>({});
  const pollTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const failCounts = useRef<Record<string, number>>({});

  // The secret is write-only: '' clears it.
  const saveWebhookSecret = async (secret: string, success: string) => {
    const cfg = await fetchApi<PlatformConfig>('/api/system/config', {
      method: 'PUT',
      body: JSON.stringify({ webhook_secret: secret }),
    });
    setWebhookSecretSet(cfg.webhook_secret_set);
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
          setWebhookSecretSet(cfg.webhook_secret_set);
        })
        .catch((err) => { setFetchErrors((prev) => ({ ...prev, config: err ?? true })); }),
    ]);

    setLoading(false);
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

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

  const saveConfig = async () => {
    try {
      const cfg = await fetchApi<PlatformConfig>('/api/system/config', {
        method: 'PUT',
        body: JSON.stringify({
          default_pair: defaultPair,
          default_timeframe: defaultTimeframe,
          max_concurrent_strategies: Number(maxStrategies),
          paper_capital: Number(paperCapital),
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

  const handleSaveConfig = () => {
    if (savedPaperCapital !== null && Number(paperCapital) !== savedPaperCapital) {
      setCapitalConfirmOpen(true);
      return;
    }
    void saveConfig();
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

  const grafanaUrl = process.env.NEXT_PUBLIC_GRAFANA_URL;

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-semibold text-ink-1">Settings</h1>

      {/* Exchange connections */}
      <Section title="Exchange connections">
        {fetchErrors.exchanges ? (
          <DataStateErrorCard error={fetchErrors.exchanges} fallback="Failed to load exchange connections" onRetry={loadData} />
        ) : loading ? (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="h-28 animate-pulse rounded-xl border border-line bg-raised" />
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
                        ? 'border-line-strong bg-surface'
                        : 'border-line bg-surface',
                      isExpanded && 'rounded-b-none',
                    )}
                  >
                    <div className="flex items-start justify-between">
                      <div className="flex items-center gap-3">
                        <div
                          className={cn(
                            'flex h-10 w-10 items-center justify-center rounded-lg text-sm font-bold',
                            exchange.connected
                              ? 'bg-raised text-ink-1'
                              : 'bg-raised text-ink-2',
                          )}
                        >
                          {exchange.logo}
                        </div>
                        <div>
                          <h3 className="text-sm font-semibold text-ink-1">
                            {exchange.name}
                          </h3>
                          <p className="mt-0.5 text-xs text-ink-2" data-testid={`validation-${exchange.id}`}>
                            {validationText(exchange)}
                          </p>
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        {detail && connectionStatusBadge(detail)}
                        {exchange.connected ? (
                          <CheckCircle className="h-5 w-5 text-ink-1" aria-label="Connected" />
                        ) : (
                          <XCircle className="h-5 w-5 text-ink-2" aria-label="Disconnected" />
                        )}
                      </div>
                    </div>
                    <div className="mt-3 flex items-center justify-between">
                      <span className="flex items-center gap-2 text-xs text-ink-2">
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
                          {exchange.connected ? 'Delete keys' : 'Connect'}
                        </Button>
                      </div>
                    </div>
                  </div>

                  {/* Expandable detail panel */}
                  {isExpanded && exchange.connected && (
                    <div className="rounded-b-xl border border-t-0 border-line bg-surface p-4 space-y-4">
                      {/* Balances */}
                      <div>
                        <h4 className="text-xs font-semibold uppercase tracking-wider text-ink-2 mb-2">Balances</h4>
                        {detail?.balances.error ? (
                          <p className="text-xs text-danger" data-testid={`balances-error-${exchange.id}`}>{detail.balances.error}</p>
                        ) : detail?.balances.data && detail.balances.data.length > 0 ? (
                          <div className="overflow-x-auto">
                            <table className="w-full text-xs">
                              <thead>
                                <tr className="border-b border-line">
                                  <th className="px-2 py-1.5 text-left text-ink-2 font-semibold">Currency</th>
                                  <th className="px-2 py-1.5 text-right text-ink-2 font-semibold">Available</th>
                                  <th className="px-2 py-1.5 text-right text-ink-2 font-semibold">Total</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-border-default">
                                {detail.balances.data.map((b) => (
                                  <tr key={b.currency}>
                                    <td className="px-2 py-1.5 text-ink-1 font-medium">{b.currency}</td>
                                    <td className="px-2 py-1.5 text-right text-ink-1">{b.available.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                                    <td className="px-2 py-1.5 text-right text-ink-1">{b.total.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        ) : (
                          <p className="text-xs text-ink-2">No balance data</p>
                        )}
                      </div>

                      {/* Open Orders */}
                      <div>
                        <h4 className="text-xs font-semibold uppercase tracking-wider text-ink-2 mb-2">Open Orders</h4>
                        {detail?.orders.error ? (
                          <p className="text-xs text-danger" data-testid={`orders-error-${exchange.id}`}>{detail.orders.error}</p>
                        ) : detail?.orders.data && detail.orders.data.length > 0 ? (
                          <div className="overflow-x-auto">
                            <table className="w-full text-xs">
                              <thead>
                                <tr className="border-b border-line">
                                  <th className="px-2 py-1.5 text-left text-ink-2 font-semibold">Symbol</th>
                                  <th className="px-2 py-1.5 text-left text-ink-2 font-semibold">Side</th>
                                  <th className="px-2 py-1.5 text-left text-ink-2 font-semibold">Type</th>
                                  <th className="px-2 py-1.5 text-right text-ink-2 font-semibold">Qty</th>
                                  <th className="px-2 py-1.5 text-right text-ink-2 font-semibold">Price</th>
                                  <th className="px-2 py-1.5 text-left text-ink-2 font-semibold">Status</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-border-default">
                                {detail.orders.data.map((o, i) => (
                                  <tr key={i}>
                                    <td className="px-2 py-1.5 text-ink-1">{o.symbol}</td>
                                    <td className="px-2 py-1.5">
                                      <StatusBadge status={o.side} variant={o.side.toLowerCase() === 'buy' ? 'success' : 'error'} size="sm" />
                                    </td>
                                    <td className="px-2 py-1.5 text-ink-1">{o.type}</td>
                                    <td className="px-2 py-1.5 text-right text-ink-1">{o.quantity ?? '-'}</td>
                                    <td className="px-2 py-1.5 text-right text-ink-1">{o.price === null ? 'Market' : `$${o.price.toLocaleString('en-US', { minimumFractionDigits: 2 })}`}</td>
                                    <td className="px-2 py-1.5">
                                      <StatusBadge status={o.status} variant="neutral" size="sm" />
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        ) : (
                          <p className="text-xs text-ink-2">No open orders</p>
                        )}
                      </div>

                      {/* Positions: a spot account has none */}
                      <div>
                        <h4 className="text-xs font-semibold uppercase tracking-wider text-ink-2 mb-2">Positions</h4>
                        <p className="text-xs text-ink-2">
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
          <p className="text-sm text-ink-2 text-center py-8">No data yet</p>
        )}
      </Section>

      <Section title="Alerts">
        <div className="space-y-2 text-sm text-ink-1">
          <p>Alerts are sent by Alertmanager to Telegram. Change them in the alert rules, not here.</p>
          <p className="text-ink-2">Webhook alerts aren&apos;t sent in this version.</p>
          {grafanaUrl && (
            <p>
              <a href={grafanaUrl} className="underline underline-offset-2" target="_blank" rel="noreferrer">
                Open Grafana
              </a>
            </p>
          )}
        </div>
      </Section>

      <Section title="Appearance">
        <div role="group" aria-label="Theme" className="flex rounded-lg border border-line bg-surface p-1 lg:inline-flex">
          {(['dark', 'light', 'system'] as const).map((t) => (
            <button
              key={t}
              type="button"
              aria-pressed={theme === t}
              onClick={() => setTheme(t)}
              className={cn(
                'h-11 flex-1 rounded-md px-4 text-sm font-medium capitalize lg:h-8',
                theme === t ? 'bg-raised text-ink-1' : 'text-ink-2 hover:text-ink-1',
              )}
            >
              {t}
            </button>
          ))}
        </div>
      </Section>

      {/* Webhook Configuration */}
      <Section title="Signal webhook">
        <div className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-ink-2 mb-1">Webhook URL</label>
            <div className="flex flex-wrap items-center gap-2">
              <div className="min-w-0 flex-1 break-all rounded-lg border border-line bg-raised px-3 py-2 text-sm text-ink-2 font-mono">
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
            placeholder={webhookSecretSet ? SECRET_PLACEHOLDER : 'Not set'}
            onChange={(e) => setWebhookSecret(e.target.value)}
            hint="Shared secret for verifying webhook payloads"
          />
          <div className="flex justify-end gap-2">
            {webhookSecretSet && (
              <Button
                variant="outline"
                size="sm"
                onClick={async () => {
                  if (!confirm(CLEAR_WEBHOOK_SECRET_WARNING)) return;
                  try {
                    await saveWebhookSecret('', 'Webhook secret cleared');
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
                  await saveWebhookSecret(webhookSecret, 'Webhook secret saved');
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
      </Section>

      {/* Platform config */}
      <Section title="Platform configuration">
        {fetchErrors.config ? (
          <DataStateErrorCard error={fetchErrors.config} fallback="Failed to load platform configuration" onRetry={loadData} />
        ) : loading ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-16 animate-pulse rounded-lg bg-raised" />
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
              <div>
                <p className="text-sm font-medium text-ink-2">Trading mode</p>
                <p className="mt-1.5 text-sm text-ink-1" data-testid="trading-mode">
                  {tradingMode === 'paper'
                    ? 'Paper. Live trading arrives with a later release and will need a one-time code to turn on.'
                    : `Stored mode is ${tradingMode ?? 'unknown'}; live trading is refused until a later release. Showing paper.`}
                </p>
              </div>
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
      </Section>
      <ConfirmDialog
        open={capitalConfirmOpen}
        title={`Change paper capital to ${paperCapital} USDT?`}
        primaryLabel="Reset ledger to new capital"
        secondaryLabel="Keep current capital"
        pendingLabel="Saving…"
        pendingHint="Closing this won't undo a save already sent."
        onConfirm={async () => {
          await saveConfig();
          setCapitalConfirmOpen(false);
        }}
        onClose={() => setCapitalConfirmOpen(false)}
      >
        <p>
          This rebases the paper ledger from {savedPaperCapital} to {paperCapital} USDT: peak, day start and carried
          P&amp;L. Drawdown and today&apos;s loss restart from 0. Trades and history are kept.
        </p>
        <p>Breaker tiers, halts and pauses don&apos;t change; reset those separately.</p>
        <p>If a held position has no mark yet, the change waits and the ledger keeps the current capital until it has one.</p>
      </ConfirmDialog>
      <ExchangeConnectDialog
        open={modalOpen}
        onClose={() => { setModalOpen(false); setSelectedExchange(null); }}
        exchange={selectedExchange}
        onUpdated={refreshExchanges}
      />
    </div>
  );
}
