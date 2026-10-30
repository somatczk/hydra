'use client';

import { useEffect, useState, type ReactNode } from 'react';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { Input } from './Input';

const MIN_REASON = 3;
const MAX_REASON = 500;

interface TypedConfirmProps {
  open: boolean;
  title: string;
  /** The facts the operator needs before acting. */
  children: ReactNode;
  /** The API's own confirmation token: RELEASE, RESET or the tier number. */
  token: string;
  primaryLabel: string;
  secondaryLabel: string;
  pendingLabel: string;
  /** A failed attempt, shown in the dialog; the typed values stay. */
  error?: string | null;
  /** Called with the trimmed reason; the caller closes the dialog on success. */
  onConfirm: (reason: string) => Promise<void>;
  onClose: () => void;
}

/** L3 confirm: the token typed exactly plus an audited reason, then the primary enables. */
export function TypedConfirm({
  open,
  title,
  children,
  token,
  primaryLabel,
  secondaryLabel,
  pendingLabel,
  error,
  onConfirm,
  onClose,
}: TypedConfirmProps) {
  const [typed, setTyped] = useState('');
  const [reason, setReason] = useState('');
  const [seen, setSeen] = useState({ token: false, reason: false });

  useEffect(() => {
    if (!open) return;
    setTyped('');
    setReason('');
    setSeen({ token: false, reason: false });
  }, [open]);

  const trimmed = reason.trim();
  const tokenOk = typed === token;
  const reasonOk = trimmed.length >= MIN_REASON && trimmed.length <= MAX_REASON;
  const tokenError = /^\d+$/.test(token)
    ? `Type ${token} to confirm.`
    : token === token.toUpperCase()
      ? `Type ${token} in capitals to confirm.`
      : `Type ${token} exactly to confirm.`;
  const reasonError =
    trimmed.length > MAX_REASON ? `Keep the reason to ${MAX_REASON} characters or fewer.` : `Add a reason of at least ${MIN_REASON} characters.`;

  return (
    <ConfirmDialog
      open={open}
      destructive
      title={title}
      primaryLabel={primaryLabel}
      secondaryLabel={secondaryLabel}
      pendingLabel={pendingLabel}
      pendingHint="Closing this won't cancel the request."
      primaryDisabled={!tokenOk || !reasonOk}
      onClose={onClose}
      onConfirm={() => onConfirm(trimmed)}
    >
      {children}
      <Input
        label={`Type ${token} to confirm`}
        value={typed}
        onChange={(e) => setTyped(e.target.value)}
        onBlur={() => setSeen((s) => ({ ...s, token: true }))}
        error={seen.token && !tokenOk ? tokenError : undefined}
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
      />
      <Input
        label="Reason"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        onBlur={() => setSeen((s) => ({ ...s, reason: true }))}
        error={seen.reason && !reasonOk ? reasonError : undefined}
        hint={`Saved in the activity log. ${MIN_REASON}–${MAX_REASON} characters. ${trimmed.length}/${MAX_REASON}`}
        autoComplete="off"
      />
      {error && (
        <p role="alert" className="text-sm font-semibold text-danger">
          {error}
        </p>
      )}
    </ConfirmDialog>
  );
}
