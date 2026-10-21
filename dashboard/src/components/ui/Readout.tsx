import type { ReactNode } from 'react';
import { cn } from './cn';

interface ReadoutProps {
  label: string;
  value: ReactNode;
  /** Age, source or state under the value. */
  note?: ReactNode;
  className?: string;
}

export function Readout({ label, value, note, className }: ReadoutProps) {
  return (
    <div className={cn('rounded-lg border border-line bg-surface p-4', className)}>
      <p className="text-xs text-ink-2">{label}</p>
      <p className="mt-1 text-xl font-semibold tabular-nums text-ink-1">{value}</p>
      {note && <p className="mt-1 text-xs text-ink-2">{note}</p>}
    </div>
  );
}
