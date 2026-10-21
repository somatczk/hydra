import { useId, type ReactNode } from 'react';
import { cn } from './cn';

interface SectionProps {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}

/** A card at ≥1024 px; full-bleed with dividers below (rounded- stays: pageHarness.sectionText walks up to it). */
export function Section({ title, action, children, className }: SectionProps) {
  const id = useId();
  return (
    <section
      aria-labelledby={id}
      className={cn(
        '-mx-4 border-y border-line bg-surface px-4 py-4 lg:mx-0 lg:rounded-lg lg:border lg:p-5',
        className,
      )}
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 id={id} className="text-sm font-semibold text-ink-1">
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}
