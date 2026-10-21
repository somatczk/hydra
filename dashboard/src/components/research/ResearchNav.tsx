import Link from 'next/link';
import { cn } from '@/components/ui/cn';

const TABS = [
  { label: 'Strategies', href: '/strategies' },
  { label: 'Backtests', href: '/backtest' },
  { label: 'Optimize', href: '/optimize' },
] as const;

/** Phone-only way between the three Research pages (the rail's children are desktop-only). */
export function ResearchNav({ current }: { current: (typeof TABS)[number]['href'] }) {
  return (
    <nav aria-label="Research" className="flex rounded-lg border border-line bg-surface p-1 lg:hidden">
      {TABS.map((tab) => (
        <Link
          key={tab.href}
          href={tab.href}
          aria-current={tab.href === current ? 'page' : undefined}
          className={cn(
            'flex h-11 flex-1 items-center justify-center rounded-md px-2 text-sm font-medium',
            tab.href === current ? 'bg-raised text-ink-1' : 'text-ink-2 hover:text-ink-1',
          )}
        >
          {tab.label}
        </Link>
      ))}
    </nav>
  );
}
