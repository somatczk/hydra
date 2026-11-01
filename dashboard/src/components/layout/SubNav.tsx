'use client';

import { usePathname } from 'next/navigation';
import Link from 'next/link';
import { cn } from '@/components/ui/cn';
import { activeItem, isActive } from './navItems';

/** <1024 px: the active destination's children (the rail shows them on desktop). */
export function SubNav() {
  const pathname = usePathname();
  const item = activeItem(pathname);
  if (!item?.children) return null;

  return (
    <nav aria-label={item.label} className="-mx-4 mb-4 overflow-x-auto border-b border-line lg:hidden">
      <ul className="flex px-2">
        {item.children.map((child) => {
          const here = isActive(pathname, child);
          return (
            <li key={child.href}>
              <Link
                href={child.href}
                aria-current={here ? 'page' : undefined}
                className={cn(
                  'flex h-11 items-center whitespace-nowrap px-3 text-sm',
                  here ? 'font-semibold text-ink-1 shadow-[inset_0_-3px_0_var(--ink-1)]' : 'text-ink-2 hover:text-ink-1',
                )}
              >
                {child.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
