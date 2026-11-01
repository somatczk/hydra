import { signed } from '@/lib/format';
import { cn } from './cn';

/** A signed amount coloured by its sign; the + / − stays, so colour is never the only cue. */
export function Pnl({ value, unit, className }: { value: number | null | undefined; unit?: string; className?: string }) {
  const text = signed(value);
  if (text === null) return <span className={cn('text-ink-2', className)}>—</span>;
  const tone = text.startsWith('+') ? 'text-up' : text.startsWith('−') ? 'text-down' : 'text-ink-2';
  return <span className={cn(tone, className)}>{unit ? `${text} ${unit}` : text}</span>;
}
