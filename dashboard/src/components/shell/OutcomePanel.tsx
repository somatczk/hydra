'use client';

import Link from 'next/link';
import { AlertTriangle, CheckCircle2, OctagonAlert, X } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { cn } from '@/components/ui/cn';
import type { Outcome, OutcomeAction } from '@/lib/outcomes';

interface OutcomePanelProps {
  outcome: Outcome | null;
  onAction: (action: OutcomeAction) => void;
  onDismiss: () => void;
  /** The action in flight, if any; its button shows the spinner. */
  busy?: OutcomeAction['kind'] | null;
}

const GLYPH = { none: CheckCircle2, caution: AlertTriangle, danger: OctagonAlert } as const;
const TONE = {
  none: { border: 'border-line', glyph: 'text-ink-2' },
  caution: { border: 'border-caution', glyph: 'text-caution' },
  danger: { border: 'border-danger', glyph: 'text-danger' },
} as const;

function Panel({ outcome, onAction, onDismiss, busy }: OutcomePanelProps & { outcome: Outcome }) {
  const Glyph = GLYPH[outcome.tone];
  return (
    <div className={cn('mb-4 flex items-start gap-3 rounded border bg-surface p-4', TONE[outcome.tone].border)}>
      <Glyph aria-hidden="true" className={cn('mt-0.5 h-5 w-5 shrink-0', TONE[outcome.tone].glyph)} />
      <div className="flex min-w-0 flex-1 flex-col gap-1 text-sm">
        <p className="font-semibold text-ink-1">{outcome.title}</p>
        {outcome.lines.map((line, i) => (
          <p key={`${i}:${line}`} className="text-ink-1">
            {line}
          </p>
        ))}
        {outcome.actions.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {outcome.actions.map((a) =>
              a.href ? (
                <Link
                  key={a.label}
                  href={a.href}
                  className="inline-flex h-8 items-center rounded border border-line-strong px-3 text-sm font-medium hover:bg-hover max-lg:h-11"
                >
                  {a.label}
                </Link>
              ) : (
                <Button
                  key={a.label}
                  variant="secondary"
                  size="sm"
                  loading={busy === a.kind}
                  disabled={!!busy}
                  onClick={() => onAction(a)}
                >
                  {a.label}
                </Button>
              ),
            )}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
        className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded text-ink-2 hover:bg-hover hover:text-ink-1 max-lg:h-11 max-lg:w-11"
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
    </div>
  );
}

/** The record of the last money action; both live regions stay mounted so injected text is announced (R-P2-12). */
export function OutcomePanel(props: OutcomePanelProps) {
  const { outcome } = props;
  const danger = outcome?.tone === 'danger';
  return (
    <>
      <div role="status" aria-live="polite" aria-label="Outcome">
        {outcome && !danger && <Panel {...props} outcome={outcome} />}
      </div>
      <div role="alert" aria-label="Outcome alert">
        {outcome && danger && <Panel {...props} outcome={outcome} />}
      </div>
    </>
  );
}
