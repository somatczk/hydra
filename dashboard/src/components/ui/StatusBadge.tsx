import { cn } from './cn';

type BadgeVariant = 'success' | 'warning' | 'error' | 'info' | 'neutral';
type BadgeSize = 'sm' | 'md' | 'lg';

interface StatusBadgeProps {
  status: string;
  variant?: BadgeVariant;
  size?: BadgeSize;
  className?: string;
}

// Chip tones (05 §3): success/info/neutral are quiet; only caution and danger take colour.
const variantStyles: Record<BadgeVariant, { dot: string; text: string; bg: string }> = {
  success: { dot: 'bg-ink-2', text: 'text-ink-1', bg: 'border border-line bg-surface' },
  warning: { dot: 'bg-caution', text: 'text-caution', bg: 'bg-caution-soft' },
  error: { dot: 'bg-danger', text: 'text-danger', bg: 'bg-danger-soft' },
  info: { dot: 'bg-ink-2', text: 'text-ink-2', bg: 'border border-line bg-surface' },
  neutral: { dot: 'bg-ink-2', text: 'text-ink-2', bg: 'bg-raised border border-line' },
};

const sizeStyles: Record<BadgeSize, { wrapper: string; dot: string; text: string }> = {
  sm: { wrapper: 'px-2 py-0.5 gap-1', dot: 'h-1.5 w-1.5', text: 'text-xs' },
  md: { wrapper: 'px-2.5 py-1 gap-1.5', dot: 'h-2 w-2', text: 'text-xs' },
  lg: { wrapper: 'px-3 py-1.5 gap-2', dot: 'h-2 w-2', text: 'text-sm' },
};

export function StatusBadge({
  status,
  variant = 'neutral',
  size = 'md',
  className,
}: StatusBadgeProps) {
  const v = variantStyles[variant];
  const s = sizeStyles[size];

  return (
    <span
      className={cn(
        'inline-flex items-center rounded font-medium',
        v.bg,
        s.wrapper,
        className,
      )}
    >
      <span
        className={cn('shrink-0 rounded-full', v.dot, s.dot)}
        aria-hidden="true"
      />
      <span className={cn(v.text, s.text)}>{status}</span>
    </span>
  );
}
