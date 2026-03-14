'use client';

import { useEffect, useState } from 'react';
import { Globe, Bell, Cog, CheckCircle, XCircle, OctagonX } from 'lucide-react';
import { DataCard } from '@/components/ui/DataCard';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Button } from '@/components/ui/Button';
import { cn } from '@/components/ui/cn';
import { fetchApi } from '@/lib/api';
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
}

interface RiskConfig {
  kill_switch_active: boolean;
}

/* ---------- Placeholder data ---------- */

const logoMap: Record<string, string> = {
  binance: 'B',
  bybit: 'BY',
  kraken: 'K',
  okx: 'O',
};

const placeholderExchanges: Exchange[] = [
  { id: 'binance', name: 'Binance', logo: 'B', connected: true, apiKeySet: true, lastSync: '2 min ago' },
  { id: 'bybit', name: 'Bybit', logo: 'BY', connected: true, apiKeySet: true, lastSync: '5 min ago' },
  { id: 'kraken', name: 'Kraken', logo: 'K', connected: false, apiKeySet: false, lastSync: 'Never' },
  { id: 'okx', name: 'OKX', logo: 'O', connected: false, apiKeySet: true, lastSync: '3 days ago' },
];

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

/* ---------- Page ---------- */

export default function SettingsPage() {
  const [exchanges, setExchanges] = useState<Exchange[]>(placeholderExchanges);
  const [config, setConfig] = useState<PlatformConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [tradingMode, setTradingMode] = useState('paper');
  const [defaultPair, setDefaultPair] = useState('btcusdt');
  const [defaultTimeframe, setDefaultTimeframe] = useState('1h');
  const [maxStrategies, setMaxStrategies] = useState('5');
  const [paperCapital, setPaperCapital] = useState('10000');
  const [saved, setSaved] = useState(false);
  const [killSwitchActive, setKillSwitchActive] = useState(false);
  const [notifications, setNotifications] = useState<Record<string, boolean>>({
    'Trade Executions': true,
    'Risk Alerts': true,
    'Model Drift': true,
    'Strategy Signals': false,
    'System Health': true,
  });
  const [selectedExchange, setSelectedExchange] = useState<Exchange | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  useEffect(() => {
    Promise.all([
      fetchApi<ApiExchange[]>('/api/system/exchanges').catch(() => null),
      fetchApi<PlatformConfig>('/api/system/config').catch(() => null),
      fetchApi<RiskConfig>('/api/risk/config').catch(() => null),
    ])
      .then(([ex, cfg, risk]) => {
        if (ex) setExchanges(mapApiExchanges(ex));
        if (cfg) {
          setConfig(cfg);
          setTradingMode(cfg.trading_mode);
          setDefaultPair(cfg.default_pair);
          setDefaultTimeframe(cfg.default_timeframe);
          setMaxStrategies(cfg.max_concurrent_strategies.toString());
        }
        if (risk) {
          setKillSwitchActive(risk.kill_switch_active);
        }
      })
      .finally(() => setLoading(false));
  }, []);

  const refreshExchanges = async () => {
    try {
      const ex = await fetchApi<ApiExchange[]>('/api/system/exchanges');
      setExchanges(mapApiExchanges(ex));
    } catch { /* ignore */ }
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
        }),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch {
      /* API unavailable */
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
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {exchanges.map((exchange) => (
            <div
              key={exchange.id}
              className={cn(
                'rounded-xl border p-4 transition-colors',
                exchange.connected
                  ? 'border-status-success/30 bg-status-success/5'
                  : 'border-border-default bg-bg-secondary',
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
                {exchange.connected ? (
                  <CheckCircle className="h-5 w-5 text-status-success" aria-label="Connected" />
                ) : (
                  <XCircle className="h-5 w-5 text-text-light" aria-label="Disconnected" />
                )}
              </div>
              <div className="mt-3 flex items-center justify-between">
                <span className="text-xs text-text-muted">
                  API Key: {exchange.apiKeySet ? 'Configured' : 'Not set'}
                </span>
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
          ))}
        </div>
      </DataCard>

      {/* Notification preferences */}
      <DataCard title="Notification Preferences" description="Configure alert and notification settings">
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
                  onClick={() => setNotifications((prev) => ({ ...prev, [pref.label]: !prev[pref.label] }))}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setNotifications((prev) => ({ ...prev, [pref.label]: !prev[pref.label] })); } }}
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
      </DataCard>

      {/* Platform config */}
      <DataCard title="Platform Configuration" description="General platform settings">
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
          {saved && (
            <span className="text-sm font-medium text-status-success">Configuration saved</span>
          )}
          <Button variant="primary" onClick={handleSaveConfig}>
            <Cog className="h-4 w-4" />
            {loading ? 'Loading...' : 'Save Configuration'}
          </Button>
        </div>
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
