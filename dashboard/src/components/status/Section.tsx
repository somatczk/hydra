import type { ReactNode } from 'react';
import { cn } from '@/components/ui/cn';

/** An h2, an action slot and content under a rule. */
export function Section({
  title,
  action,
  id,
  className,
  children,
}: {
  title: string;
  action?: ReactNode;
  id?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section
      id={id}
      aria-labelledby={id ? `${id}-h` : undefined}
      className={cn(
        'border-t border-line pt-4',
        className,
      )}
    >
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 id={id ? `${id}-h` : undefined} className="text-xl font-semibold text-ink-1">
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}
