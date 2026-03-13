'use client';

import { cn } from './cn';

/* ---------- Types ---------- */

interface Column<T> {
  key: string;
  header: string;
  render?: (row: T) => React.ReactNode;
  hideOnMobile?: boolean;
  mobileLabel?: string;
}

interface TableProps<T> {
  columns: Column<T>[];
  data: T[];
  keyExtractor: (row: T) => string;
  onRowClick?: (row: T) => void;
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
          <div className="h-4 w-3/4 animate-pulse rounded bg-bg-tertiary" />
        </td>
      ))}
    </tr>
  );
}

/* ---------- Mobile Card ---------- */

function MobileCard<T>({
  columns,
  row,
  onClick,
}: {
  columns: Column<T>[];
  row: T;
  onClick?: (row: T) => void;
}) {
  const getValue = (col: Column<T>) => {
    if (col.render) return col.render(row);
    return String((row as Record<string, unknown>)[col.key] ?? '');
  };

  return (
    <button
      type="button"
      className={cn(
        'w-full rounded-xl border border-border-default bg-bg-elevated p-4 text-left',
        onClick && 'cursor-pointer hover:bg-bg-hover transition-colors',
      )}
      onClick={() => onClick?.(row)}
      disabled={!onClick}
    >
      <div className="space-y-2">
        {columns.map((col) => (
          <div key={col.key} className="flex items-center justify-between gap-4">
            <span className="text-xs font-medium text-text-muted">
              {col.mobileLabel ?? col.header}
            </span>
            <span className="text-sm text-text-primary text-right">
              {getValue(col)}
            </span>
          </div>
        ))}
      </div>
    </button>
  );
}

/* ---------- Table ---------- */

export function Table<T>({
  columns,
  data,
  keyExtractor,
  onRowClick,
  emptyMessage = 'No data available',
  loading = false,
  className,
}: TableProps<T>) {
  const getValue = (col: Column<T>, row: T) => {
    if (col.render) return col.render(row);
    return String((row as Record<string, unknown>)[col.key] ?? '');
  };

  /* Empty state */
  if (!loading && data.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <p className="text-sm text-text-muted">{emptyMessage}</p>
      </div>
    );
  }

  return (
    <>
      {/* Mobile card layout */}
      <div className={cn('flex flex-col gap-3 md:hidden', className)}>
        {loading
          ? Array.from({ length: 3 }).map((_, i) => (
              <div
                key={i}
                className="h-28 animate-pulse rounded-xl border border-border-default bg-bg-tertiary"
              />
            ))
          : data.map((row) => (
              <MobileCard
                key={keyExtractor(row)}
                columns={columns}
                row={row}
                onClick={onRowClick}
              />
            ))}
      </div>

      {/* Desktop table */}
      <div className={cn('hidden md:block overflow-x-auto', className)}>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border-default">
              {columns
                .filter((c) => !c.hideOnMobile)
                .map((col) => (
                  <th
                    key={col.key}
                    className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-muted"
                  >
                    {col.header}
                  </th>
                ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border-default">
            {loading
              ? Array.from({ length: 5 }).map((_, i) => (
                  <SkeletonRow
                    key={i}
                    cols={columns.filter((c) => !c.hideOnMobile).length}
                  />
                ))
              : data.map((row) => (
                  <tr
                    key={keyExtractor(row)}
                    className={cn(
                      'transition-colors',
                      onRowClick &&
                        'cursor-pointer hover:bg-bg-hover',
                    )}
                    onClick={() => onRowClick?.(row)}
                  >
                    {columns
                      .filter((c) => !c.hideOnMobile)
                      .map((col) => (
                        <td
                          key={col.key}
                          className="px-4 py-3 text-text-primary"
                        >
                          {getValue(col, row)}
                        </td>
                      ))}
                  </tr>
                ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
