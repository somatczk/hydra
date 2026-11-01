import { useId, type ReactNode } from 'react';
import { cn } from './cn';

interface SectionProps {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}

/** An h2, an action slot and content under a rule. */
export function Section({ title, action, children, className }: SectionProps) {
  const id = useId();
  return (
    <section
      aria-labelledby={id}
      className={cn(
        'border-t border-line pt-4 pb-2',
        className,
      )}
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 id={id} className="text-xl font-semibold text-ink-1">
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}
