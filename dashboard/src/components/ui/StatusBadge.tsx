import { cn } from './cn';

type BadgeVariant = 'success' | 'warning' | 'error' | 'info' | 'neutral';
type BadgeSize = 'sm' | 'md' | 'lg';

interface StatusBadgeProps {
  status: string;
  variant?: BadgeVariant;
  size?: BadgeSize;
  className?: string;
}

const variantStyles: Record<BadgeVariant, { dot: string; text: string; bg: string }> = {
  success: {
    dot: 'bg-status-success',
    text: 'text-status-success',
    bg: 'bg-status-success/10',
  },
  warning: {
    dot: 'bg-status-warning',
    text: 'text-status-warning',
    bg: 'bg-status-warning/10',
  },
  error: {
    dot: 'bg-status-error',
    text: 'text-status-error',
    bg: 'bg-status-error/10',
  },
  info: {
    dot: 'bg-status-info',
    text: 'text-status-info',
    bg: 'bg-status-info/10',
  },
  neutral: {
    dot: 'bg-text-muted',
    text: 'text-text-muted',
    bg: 'bg-bg-tertiary',
  },
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
        'inline-flex items-center rounded-full font-medium',
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
