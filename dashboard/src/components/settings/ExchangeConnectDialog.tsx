'use client';

import { useState, useEffect } from 'react';
import { X, Plug, Unplug } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { logger } from '@/lib/logger';

interface Exchange {
  id: string;
  name: string;
  connected: boolean;
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
  const [saving, setSaving] = useState(false);
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
    setSaving(true);
    setError(null);
    try {
      await fetchApi(`/api/system/exchanges/${exchange.id}/connect`, {
        method: 'POST',
        body: JSON.stringify({
          api_key: apiKey.trim(),
          api_secret: apiSecret.trim(),
          ...(passphrase.trim() ? { passphrase: passphrase.trim() } : {}),
        }),
      });
      logger.info('Settings', `Exchange ${exchange.name} connected`);
      toast('success', `${exchange.name} connected`);
      onUpdated();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Connection failed');
    } finally {
      setSaving(false);
    }
  };

  const handleDisconnect = async () => {
    setSaving(true);
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
      setError(err instanceof Error ? err.message : 'Disconnect failed');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} aria-hidden="true" />
      <div className="relative w-full max-w-lg rounded-xl border border-border-default bg-bg-elevated p-6 shadow-xl mx-4">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-text-primary">
            {exchange.connected ? `Manage ${exchange.name}` : `Connect ${exchange.name}`}
          </h3>
          <button type="button" onClick={onClose} className="rounded-lg p-1 text-text-muted hover:text-text-primary hover:bg-bg-hover transition-colors" aria-label="Close dialog">
            <X className="h-5 w-5" />
          </button>
        </div>

        {exchange.connected ? (
          /* Manage mode */
          <div className="flex flex-col gap-4">
            <div className="rounded-lg border border-status-success/30 bg-status-success/5 p-4">
              <p className="text-sm font-medium text-status-success">Connected</p>
              <p className="text-xs text-text-muted mt-1">API credentials are configured for {exchange.name}</p>
            </div>
            {error && (
              <div className="rounded-lg border border-status-error/30 bg-status-error/5 p-3">
                <p className="text-sm text-status-error">{error}</p>
              </div>
            )}
            <div className="flex justify-end gap-3 mt-2">
              <Button variant="outline" onClick={onClose} disabled={saving}>Close</Button>
              <Button variant="danger" onClick={handleDisconnect} loading={saving}>
                <Unplug className="h-4 w-4" />
                Disconnect
              </Button>
            </div>
          </div>
        ) : (
          /* Connect mode */
          <div className="flex flex-col gap-4">
            <Input label="API Key" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="Enter API key" autoFocus />
            <Input label="API Secret" type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} placeholder="Enter API secret" />
            <Input label="Passphrase (optional)" type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} placeholder="Required for some exchanges" />
            {error && (
              <div className="rounded-lg border border-status-error/30 bg-status-error/5 p-3">
                <p className="text-sm text-status-error">{error}</p>
              </div>
            )}
            <div className="flex justify-end gap-3 mt-2">
              <Button variant="outline" onClick={onClose} disabled={saving}>Cancel</Button>
              <Button variant="primary" onClick={handleConnect} loading={saving}>
                <Plug className="h-4 w-4" />
                Connect
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
