import { type ReactNode } from 'react';
import { TrendingUp, TrendingDown, Minus, type LucideIcon } from 'lucide-react';
import { cn } from './cn';

/* ---------- DataCard ---------- */

interface DataCardProps {
  title?: string;
  description?: string;
  children: ReactNode;
  padding?: 'sm' | 'md' | 'lg';
  className?: string;
}

const paddingStyles = {
  sm: 'p-3',
  md: 'p-4 md:p-5',
  lg: 'p-5 md:p-6',
} as const;

export function DataCard({
  title,
  description,
  children,
  padding = 'md',
  className,
}: DataCardProps) {
  return (
    <div
      className={cn(
        'rounded-xl border border-border-default bg-bg-elevated',
        paddingStyles[padding],
        className,
      )}
    >
      {(title || description) && (
        <div className="mb-3">
          {title && (
            <h3 className="text-sm font-semibold text-text-primary">{title}</h3>
          )}
          {description && (
            <p className="mt-0.5 text-xs text-text-muted">{description}</p>
          )}
        </div>
      )}
      {children}
    </div>
  );
}

/* ---------- StatCard ---------- */

type ChangeType = 'increase' | 'decrease' | 'neutral';

interface StatCardProps {
  icon: LucideIcon;
  label: string;
  value: string;
  change?: number;
  changeType?: ChangeType;
  className?: string;
}

const changeStyles: Record<ChangeType, string> = {
  increase: 'text-status-success',
  decrease: 'text-status-error',
  neutral: 'text-text-muted',
};

const ChangeIcon: Record<ChangeType, LucideIcon> = {
  increase: TrendingUp,
  decrease: TrendingDown,
  neutral: Minus,
};

export function StatCard({
  icon: Icon,
  label,
  value,
  change,
  changeType = 'neutral',
  className,
}: StatCardProps) {
  const ChangeArrow = ChangeIcon[changeType];

  return (
    <div
      className={cn(
        'rounded-xl border border-border-default bg-bg-elevated p-4 md:p-5',
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-bg-tertiary">
          <Icon className="h-4 w-4 text-text-muted" aria-hidden="true" />
        </div>
        {change !== undefined && (
          <div
            className={cn(
              'flex items-center gap-0.5 text-xs font-medium',
              changeStyles[changeType],
            )}
          >
            <ChangeArrow className="h-3 w-3" aria-hidden="true" />
            <span>{Math.abs(change)}%</span>
          </div>
        )}
      </div>
      <div className="mt-3">
        <p className="text-xs font-medium text-text-muted">{label}</p>
        <p className="mt-0.5 text-xl font-bold text-text-primary font-display">
          {value}
        </p>
      </div>
    </div>
  );
}
