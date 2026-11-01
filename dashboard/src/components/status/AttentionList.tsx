import type { ReactNode } from 'react';
import Link from 'next/link';
import { DataStateBadge } from '@/components/ui/DataStateBadge';
import type { NeedsYou } from '@/lib/derive';

/** The Needs-you rows, numbered as a queue: each an `<a>`; "?" never becomes "nothing". */
export function AttentionList({
  needs,
  loading,
  history,
}: {
  needs: NeedsYou;
  loading: boolean;
  history: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3">
      {loading && needs.count === null ? (
        <p className="text-sm text-ink-2">Checking…</p>
      ) : (
        <>
          {needs.items.length > 0 && (
            <ol className="flex flex-col border-t-2 border-ink-1">
              {needs.items.map((n, i) => (
                <li key={`${n.kind}:${n.href}`} className="border-b border-line">
                  <Link href={n.href} className="flex min-h-12 items-baseline gap-3 py-2.5 text-sm text-ink-1 hover:bg-hover">
                    <span aria-hidden="true" className="font-display text-2xl font-semibold leading-none text-ink-2">{i + 1}</span>
                    {n.text}
                  </Link>
                </li>
              ))}
            </ol>
          )}
          {needs.count === null ? (
            <p className="flex flex-wrap items-center gap-2 text-sm text-ink-1">
              <DataStateBadge state="unknown" />
              Couldn&apos;t check everything: {needs.unavailable.join(', ')} unavailable
            </p>
          ) : (
            needs.count === 0 && <p className="text-sm text-ink-1">Nothing needs you.</p>
          )}
        </>
      )}
      <p className="text-xs text-ink-2">{history}</p>
    </div>
  );
}
