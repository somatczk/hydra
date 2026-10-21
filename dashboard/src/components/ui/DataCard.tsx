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
        'rounded-lg border border-line bg-surface',
        paddingStyles[padding],
        className,
      )}
    >
      {(title || description) && (
        <div className="mb-3">
          {title && (
            <h3 className="text-sm font-semibold text-ink-1">{title}</h3>
          )}
          {description && (
            <p className="mt-0.5 text-xs text-ink-2">{description}</p>
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
  increase: 'text-ink-2',
  decrease: 'text-ink-2',
  neutral: 'text-ink-2',
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
        'rounded-lg border border-line bg-surface p-4 md:p-5',
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-raised">
          <Icon className="h-4 w-4 text-ink-2" aria-hidden="true" />
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
        <p className="text-xs font-medium text-ink-2">{label}</p>
        <p className="mt-0.5 text-xl font-bold text-ink-1 font-display">
          {value}
        </p>
      </div>
    </div>
  );
}
