import type { ReactNode } from 'react';
import Link from 'next/link';
import { DataStateBadge } from '@/components/ui/DataStateBadge';
import type { NeedsYou } from '@/lib/derive';

/** The Needs-you rows (06 §2): each an `<a>`; "?" never becomes "nothing". */
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
            <ul className="flex flex-col divide-y divide-line rounded-lg border border-line">
              {needs.items.map((n) => (
                <li key={`${n.kind}:${n.href}`}>
                  <Link href={n.href} className="flex min-h-12 items-center px-3 py-2 text-sm text-ink-1 hover:bg-hover">
                    {n.text}
                  </Link>
                </li>
              ))}
            </ul>
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
