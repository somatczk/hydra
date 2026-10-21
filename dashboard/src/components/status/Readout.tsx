import type { ReactNode } from 'react';
import Link from 'next/link';
import { cn } from '@/components/ui/cn';
import { DataStateBadge } from '@/components/ui/DataStateBadge';
import type { DataState } from '@/lib/apiState';
import type { Tone } from '@/lib/derive';

const TONE: Record<Tone, string> = { none: 'text-ink-1', caution: 'text-caution', danger: 'text-danger' };

/** One labelled value. `value: null` is loading; a non-ok `state` adds its badge so a default never reads as data. */
export function Readout({
  label,
  value,
  sub,
  state = 'ok',
  tone = 'none',
  href,
  className,
}: {
  label: string;
  value: ReactNode | null;
  sub?: ReactNode;
  state?: DataState;
  tone?: Tone;
  href?: string;
  className?: string;
}) {
  const body = (
    <>
      <span className="text-xs text-ink-2">{label}</span>
      {value === null ? (
        <span className="flex flex-col gap-1" role="status">
          <span aria-hidden="true" className="h-5 w-24 animate-pulse rounded bg-hover" />
          <span className="sr-only">Loading</span>
        </span>
      ) : (
        <span className={cn('flex flex-wrap items-center gap-2 text-sm font-semibold', TONE[tone])}>
          <DataStateBadge state={state} />
          {value}
        </span>
      )}
      {sub && <span className="text-xs text-ink-2">{sub}</span>}
    </>
  );
  const box = cn('flex min-h-12 flex-col gap-1 rounded-lg border border-line bg-surface p-3', className);
  return href ? (
    <Link href={href} className={cn(box, 'hover:bg-hover')}>
      {body}
    </Link>
  ) : (
    <div className={box}>{body}</div>
  );
}
