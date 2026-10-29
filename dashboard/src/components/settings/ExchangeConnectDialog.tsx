'use client';

import { useEffect, useRef, useState } from 'react';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { Input } from '@/components/ui/Input';
import { DataStateBadge } from '@/components/ui/DataStateBadge';
import { settle } from '@/components/sessions/useOutcomeSlot';
import { fetchApi } from '@/lib/api';
import { describeApiError, parseErrorDetail } from '@/lib/apiState';
import { useToast } from '@/components/ui/Toast';
import { logger } from '@/lib/logger';
import { utc } from '@/lib/format';
import { credentialDeleteOutcome, type ApproveResponse, type DenyResponse, type LiveApprovalChallenge, type Outcome } from '@/lib/outcomes';

interface Exchange {
  id: string;
  name: string;
  connected: boolean;
  credentialState?: 'ok' | 'undecryptable' | 'missing';
  environment: 'testnet' | 'mainnet';
  /** R9: true when the stored keys can trade (validated_permissions); null/absent = read-only. */
  tradeEnabled?: boolean | null;
}

interface ConnectResponse {
  message: string;
}

const CODE_LENGTH = 6;
const CODE_RE = /^\d{6}$/;

function Failure({ outcome }: { outcome: Outcome }) {
  return (
    <div role="alert" className={outcome.tone === 'danger' ? 'text-danger' : 'text-caution'}>
      <p className="font-semibold">{outcome.title}</p>
      {outcome.lines.map((l) => (
        <p key={l}>{l}</p>
      ))}
    </div>
  );
}

interface ExchangeConnectDialogProps {
  open: boolean;
  onClose: () => void;
  exchange: Exchange | null;
  onUpdated: () => void;
}

/** The perps venue's connect copy: its keys are the live perp trading keys. */
const isPerpsVenue = (id: string) => id === 'binanceusdm';

export function ExchangeConnectDialog({ open, onClose, exchange, onUpdated }: ExchangeConnectDialogProps) {
  const { toast } = useToast();
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState('');
  const [code, setCode] = useState('');
  const [codeError, setCodeError] = useState<string | null>(null);
  const [challenge, setChallenge] = useState<LiveApprovalChallenge | null>(null);
  const [down, setDown] = useState(false);
  const [deleteFailure, setDeleteFailure] = useState<Outcome | null>(null);
  const [creating, setCreating] = useState(true);
  const codeRef = useRef<HTMLInputElement>(null);
  const hadCodeError = useRef(false);
  const deciding = useRef(false);

  const l4 = open && exchange !== null && exchange.connected && exchange.tradeEnabled === true;
  const deleteBody = l4 && exchange !== null ? JSON.stringify({ exchange_id: exchange.id, reason: `Delete the stored ${exchange.name} keys` }) : null;

  useEffect(() => {
    if (open) {
      setApiKey('');
      setApiSecret('');
      setPassphrase('');
      setError(null);
      setToken('');
      setCode('');
      setCodeError(null);
      setChallenge(null);
      setDown(false);
      setDeleteFailure(null);
    }
  }, [open, exchange?.id]);

  // R9's create-route: the credential_delete challenge is bound to this exchange's keys.
  useEffect(() => {
    if (deleteBody === null) return;
    let alive = true;
    setCreating(true);
    void settle(fetchApi<LiveApprovalChallenge>('/api/live/credential-delete', { method: 'POST', body: deleteBody })).then((result) => {
      if (!alive) return;
      setCreating(false);
      if ('ok' in result) {
        setChallenge(result.ok);
        return;
      }
      const d = parseErrorDetail(result.err);
      if (d === null || d.state === 'unavailable') setDown(true);
      else setDeleteFailure(credentialDeleteOutcome({ err: result.err }, 'challenge'));
    });
    return () => {
      alive = false;
    };
  }, [deleteBody]);

  useEffect(() => {
    if (codeError && !hadCodeError.current) codeRef.current?.focus();
    hadCodeError.current = codeError !== null;
  }, [codeError]);

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

  const confirmDelete = async () => {
    if (!challenge) return;
    deciding.current = true;
    try {
      const result = await settle(
        fetchApi<ApproveResponse>(`/api/activity/approvals/${challenge.approval_id}/approve`, {
          method: 'POST',
          body: JSON.stringify({ code }),
        }),
      );
      const d = 'err' in result ? parseErrorDetail(result.err) : null;
      if (d?.code === 'approval_expired') {
        setChallenge(null);
        setCode('');
        setCodeError(null);
        setDeleteFailure({
          tone: 'caution',
          title: `The one-time code expired at ${utc(challenge.expires_at) ?? 'its expiry'}. Nothing changed.`,
          lines: ['Close this and delete again for a fresh code.'],
          actions: [],
        });
        return;
      }
      const outcome = credentialDeleteOutcome(result, 'approve');
      if ('ok' in result && result.ok.executed && !outcome.unanswered) {
        logger.info('Settings', `Exchange ${exchange.name} disconnected`);
        toast('info', `${exchange.name} disconnected`);
        onUpdated();
        onClose();
        return;
      }
      if (outcome.inline) {
        setCodeError([outcome.title, ...outcome.lines].join(' '));
        return;
      }
      setChallenge(null);
      setCode('');
      setCodeError(null);
      setDeleteFailure(outcome);
    } finally {
      deciding.current = false;
    }
  };

  // A1: Keep keys (or a dialog close) denies a still-pending challenge — never one being decided.
  const keepKeys = () => {
    const id = challenge?.approval_id;
    if (id && !deciding.current) {
      void settle(fetchApi<DenyResponse>(`/api/activity/approvals/${id}/deny`, { method: 'POST', body: '{}' })).then((r) => {
        if ('err' in r) logger.warn('Settings', `Denying the abandoned delete challenge ${id} failed`, r.err);
      });
    }
    onClose();
  };

  const failure = error && (
    <p role="alert" className="rounded-lg border border-danger bg-danger-soft p-3 text-sm text-danger">
      {error}
    </p>
  );

  if (exchange.connected && exchange.tradeEnabled === true) {
    return (
      <ConfirmDialog
        open
        destructive
        title={`Delete the stored ${exchange.name} keys?`}
        primaryLabel="Delete keys"
        secondaryLabel="Keep keys"
        pendingLabel="Deleting…"
        pendingHint="Closing this won't stop the delete."
        primaryDisabled={challenge === null || token !== exchange.name.toUpperCase() || !CODE_RE.test(code)}
        onConfirm={confirmDelete}
        onClose={keepKeys}
      >
        <p>
          Balance and open orders stop showing here. Paper sessions aren&apos;t affected. You&apos;ll need the keys to
          connect again.
        </p>
        <p>These keys can trade, so deleting them also needs the one-time code.</p>
        {challenge ? (
          <>
            <p className="text-sm text-ink-1">
              One-time code:{' '}
              <span className="font-mono text-base font-semibold tracking-widest">{challenge.code}</span> — enter it once
              to delete the keys.
            </p>
            <p className="text-xs text-ink-2">The code expires {utc(challenge.expires_at) ?? '—'}.</p>
          </>
        ) : down ? (
          <p role="alert" className="text-sm font-semibold text-caution">
            <DataStateBadge state="unavailable" reason="The code service is unavailable" /> The keys weren&apos;t
            deleted.
          </p>
        ) : (
          // A3≡S4: the getting-the-code line only shows while the create is actually in flight.
          creating ? <p className="text-sm text-ink-2">Getting the one-time code…</p> : null
        )}
        <Input
          label={`Type ${exchange.name.toUpperCase()} to confirm`}
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          hint="The exchange name, exactly as shown."
        />
        <Input
          ref={codeRef}
          label="One-time code"
          value={code}
          onChange={(e) => {
            setCode(e.target.value.replace(/\D/g, '').slice(0, CODE_LENGTH));
            setCodeError(null);
          }}
          autoComplete="one-time-code"
          inputMode="numeric"
          pattern="[0-9]*"
          maxLength={CODE_LENGTH}
          error={codeError ?? undefined}
          className="font-mono tracking-widest"
        />
        {deleteFailure && <Failure outcome={deleteFailure} />}
      </ConfirmDialog>
    );
  }

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
      {isPerpsVenue(exchange.id) ? (
        <>
          <p>
            Use a <strong>trade-only</strong> Binance USD⌂-M key: withdrawals disabled and futures
            enabled. Hydra checks it with a read-only call — and refuses keys that cannot trade
            futures — before storing it, encrypted. The perps arm ceremony re-checks the futures
            permission live before anything is armed.
          </p>
          <p className="text-xs text-ink-2">
            These keys are what a live perp session trades with; on testnet they come from
            testnet.binance.vision, on mainnet from your account. Spot sessions are unaffected.
          </p>
        </>
      ) : (
      <p>
        Use a <strong>read-only</strong> API key. Hydra checks it with a read-only call before storing it, encrypted. In
        this version it&apos;s used only to show your balance and open orders.
      </p>
      )}
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
