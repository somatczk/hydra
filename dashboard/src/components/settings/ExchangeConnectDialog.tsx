'use client';

import { useState, useEffect } from 'react';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { Input } from '@/components/ui/Input';
import { fetchApi } from '@/lib/api';
import { describeApiError } from '@/lib/apiState';
import { useToast } from '@/components/ui/Toast';
import { logger } from '@/lib/logger';

interface Exchange {
  id: string;
  name: string;
  connected: boolean;
  credentialState?: 'ok' | 'undecryptable' | 'missing';
  environment: 'testnet' | 'mainnet';
}

interface ConnectResponse {
  message: string;
}

interface ExchangeConnectDialogProps {
  open: boolean;
  onClose: () => void;
  exchange: Exchange | null;
  onUpdated: () => void;
}

export function ExchangeConnectDialog({ open, onClose, exchange, onUpdated }: ExchangeConnectDialogProps) {
  const { toast } = useToast();
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setApiKey('');
      setApiSecret('');
      setPassphrase('');
      setError(null);
    }
  }, [open, exchange?.id]);

  if (!open || !exchange) return null;

  const handleConnect = async () => {
    if (!apiKey.trim() || !apiSecret.trim()) {
      setError('API Key and Secret are required');
      return;
    }
    setError(null);
    try {
      const res = await fetchApi<ConnectResponse>(`/api/system/exchanges/${exchange.id}/connect`, {
        method: 'POST',
        body: JSON.stringify({
          api_key: apiKey.trim(),
          api_secret: apiSecret.trim(),
          ...(passphrase.trim() ? { passphrase: passphrase.trim() } : {}),
        }),
      });
      logger.info('Settings', `Exchange ${exchange.name} connected`);
      toast('success', res.message);
      onUpdated();
      onClose();
    } catch (err) {
      logger.warn('Settings', `Exchange ${exchange.name} connect refused`, err);
      setError(describeApiError(err, 'Connection failed'));
    }
  };

  const handleDisconnect = async () => {
    setError(null);
    try {
      await fetchApi(`/api/system/exchanges/${exchange.id}/connect`, {
        method: 'DELETE',
      });
      logger.info('Settings', `Exchange ${exchange.name} disconnected`);
      toast('info', `${exchange.name} disconnected`);
      onUpdated();
      onClose();
    } catch (err) {
      logger.warn('Settings', `Exchange ${exchange.name} disconnect failed`, err);
      setError(describeApiError(err, 'Disconnect failed'));
    }
  };

  const failure = error && (
    <p role="alert" className="rounded-lg border border-danger bg-danger-soft p-3 text-sm text-danger">
      {error}
    </p>
  );

  if (exchange.connected) {
    return (
      <ConfirmDialog
        open
        destructive
        title={`Delete the stored ${exchange.name} keys?`}
        primaryLabel="Delete keys"
        secondaryLabel="Keep keys"
        pendingLabel="Deleting…"
        pendingHint="Closing this won't stop the delete."
        onConfirm={handleDisconnect}
        onClose={onClose}
      >
        <p>
          Balance and open orders stop showing here. Paper sessions aren&apos;t affected. You&apos;ll need the keys to
          connect again.
        </p>
        {failure}
      </ConfirmDialog>
    );
  }

  return (
    <ConfirmDialog
      open
      title={`Connect ${exchange.name}`}
      primaryLabel="Check and save keys"
      secondaryLabel="Cancel"
      pendingLabel={`Checking with ${exchange.name}…`}
      pendingHint="Closing this won't cancel the check."
      onConfirm={handleConnect}
      onClose={onClose}
    >
      <p>
        Use a <strong>read-only</strong> API key. Hydra checks it with a read-only call before storing it, encrypted. In
        this version it&apos;s used only to show your balance and open orders.
      </p>
      <p className="text-xs text-ink-2" data-testid="connect-target">
        The keys are checked against {exchange.name} {exchange.environment}
        {exchange.environment === 'testnet'
          ? ' with keys from testnet.binance.vision; mainnet keys are rejected there.'
          : ' with keys from your mainnet account.'}
      </p>
      {exchange.credentialState === 'undecryptable' && (
        <p className="text-xs text-caution">
          The stored keys cannot be decrypted with the current credential key; enter them again.
        </p>
      )}
      <Input label="API Key" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="Enter API key" autoComplete="off" />
      <Input label="API Secret" type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} placeholder="Enter API secret" autoComplete="off" />
      <Input label="Passphrase (optional)" type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} placeholder="Required for some exchanges" autoComplete="off" />
      {failure}
    </ConfirmDialog>
  );
}
