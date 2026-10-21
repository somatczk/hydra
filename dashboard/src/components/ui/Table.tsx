'use client';

import { cn } from './cn';

/* ---------- Types ---------- */

interface Column<T> {
  key: string;
  header: string;
  render?: (row: T) => React.ReactNode;
  hideOnMobile?: boolean;
  mobileLabel?: string;
  /** Right-aligned, tabular figures. */
  numeric?: boolean;
}

interface TableProps<T> {
  columns: Column<T>[];
  data: T[];
  keyExtractor: (row: T) => string;
  /** A row that navigates is a link, never a click handler on a non-interactive element. */
  rowHref?: (row: T) => string | undefined;
  emptyMessage?: string;
  loading?: boolean;
  className?: string;
}

/* ---------- Skeleton ---------- */

function SkeletonRow({ cols }: { cols: number }) {
  return (
    <tr>
      {Array.from({ length: cols }).map((_, i) => (
        <td key={i} className="px-4 py-3">
          <div className="h-4 w-3/4 animate-pulse rounded bg-raised" />
        </td>
      ))}
    </tr>
  );
}

function cellValue<T>(col: Column<T>, row: T): React.ReactNode {
  if (col.render) return col.render(row);
  return String((row as Record<string, unknown>)[col.key] ?? '');
}

/* ---------- Mobile Card ---------- */

function MobileCard<T>({ columns, row, href }: { columns: Column<T>[]; row: T; href?: string }) {
  const body = (
    <div className="space-y-2">
      {columns
        .filter((col) => !col.hideOnMobile)
        .map((col) => (
          <div key={col.key} className="flex items-center justify-between gap-4">
            <span className="text-xs font-medium text-ink-2">{col.mobileLabel ?? col.header}</span>
            <span className="min-w-0 break-words text-right text-sm text-ink-1">{cellValue(col, row)}</span>
          </div>
        ))}
    </div>
  );
  return (
    <li className="rounded-lg border border-line bg-surface">
      {href ? (
        <a href={href} className="block rounded-lg p-4 transition-colors hover:bg-hover">
          {body}
        </a>
      ) : (
        <div className="p-4">{body}</div>
      )}
    </li>
  );
}

/* ---------- Table ---------- */

export function Table<T>({
  columns,
  data,
  keyExtractor,
  rowHref,
  emptyMessage = 'No data available',
  loading = false,
  className,
}: TableProps<T>) {
  if (!loading && data.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <p className="text-sm text-ink-2">{emptyMessage}</p>
      </div>
    );
  }

  return (
    <>
      {/* Phone: one card per row, only the columns not marked hideOnMobile */}
      <ul className={cn('flex flex-col gap-3 md:hidden', className)}>
        {loading
          ? Array.from({ length: 3 }).map((_, i) => (
              <li key={i} aria-hidden="true" className="h-28 animate-pulse rounded-lg border border-line bg-raised" />
            ))
          : data.map((row) => (
              <MobileCard key={keyExtractor(row)} columns={columns} row={row} href={rowHref?.(row)} />
            ))}
      </ul>

      {/* Desktop: every column */}
      <div className={cn('hidden overflow-x-auto md:block', className)}>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-line">
              {columns.map((col) => (
                <th
                  key={col.key}
                  scope="col"
                  className={cn(
                    'px-4 py-3 text-xs font-semibold uppercase tracking-wider text-ink-2',
                    col.numeric ? 'text-right' : 'text-left',
                  )}
                >
                  {col.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {loading
              ? Array.from({ length: 5 }).map((_, i) => <SkeletonRow key={i} cols={columns.length} />)
              : data.map((row) => {
                  const href = rowHref?.(row);
                  return (
                    <tr key={keyExtractor(row)} className={cn('transition-colors', href && 'hover:bg-hover')}>
                      {columns.map((col, i) => (
                        <td
                          key={col.key}
                          className={cn('px-4 py-3 text-ink-1', col.numeric && 'text-right tabular-nums')}
                        >
                          {href && i === 0 ? (
                            <a href={href} className="underline-offset-2 hover:underline">
                              {cellValue(col, row)}
                            </a>
                          ) : (
                            cellValue(col, row)
                          )}
                        </td>
                      ))}
                    </tr>
                  );
                })}
          </tbody>
        </table>
      </div>
    </>
  );
}
