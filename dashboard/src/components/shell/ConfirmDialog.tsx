'use client';

import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { Button } from '@/components/ui/Button';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** The facts the operator needs before acting. */
  children: ReactNode;
  primaryLabel: string;
  secondaryLabel: string;
  pendingLabel: string;
  /** Shown while pending, e.g. "Closing this won't cancel the stop." */
  pendingHint: string;
  onConfirm: () => Promise<void>;
  onClose: () => void;
  /** Focus the title on open so a stray Enter can't confirm (R-P2-11). */
  destructive?: boolean;
}

/** L2 confirm: a centred dialog at ≥1024 px, a bottom sheet below. */
export function ConfirmDialog({
  open,
  title,
  children,
  primaryLabel,
  secondaryLabel,
  pendingLabel,
  pendingHint,
  onConfirm,
  onClose,
  destructive = false,
}: ConfirmDialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const pendingRef = useRef(false);
  const [pending, setPending] = useState(false);
  const titleId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog || !open) return;
    const trigger = document.activeElement as HTMLElement | null;
    dialog.showModal();
    const field = dialog.querySelector<HTMLElement>('input, select, textarea');
    (destructive ? titleRef.current : field ?? primaryRef.current)?.focus();
    return () => {
      dialog.close();
      trigger?.focus();
    };
  }, [open, destructive]);

  const confirm = async () => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    setPending(true);
    try {
      await onConfirm();
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault();
        if (!pendingRef.current) onClose();
      }}
      className="w-full max-w-md rounded border border-line bg-surface p-6 text-ink-1 backdrop:bg-black/50 max-lg:mb-0 max-lg:max-w-full max-lg:rounded-b-none max-lg:p-4"
    >
      <h2 id={titleId} ref={titleRef} tabIndex={-1} className="text-lg font-semibold outline-none">
        {title}
      </h2>
      <div className="mt-2 flex flex-col gap-2 text-sm">{children}</div>
      <div className="mt-6 flex flex-col gap-2 lg:flex-row-reverse">
        <Button ref={primaryRef} size="lg" className="max-lg:w-full" loading={pending} onClick={() => void confirm()}>
          {pending ? pendingLabel : primaryLabel}
        </Button>
        <Button variant="ghost" size="lg" className="max-lg:w-full" onClick={onClose}>
          {pending ? 'Close' : secondaryLabel}
        </Button>
      </div>
      {pending && <p className="mt-2 text-xs text-ink-2">{pendingHint}</p>}
    </dialog>
  );
}
