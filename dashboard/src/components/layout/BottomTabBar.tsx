'use client';

import { usePathname } from 'next/navigation';
import Link from 'next/link';
import { cn } from '@/components/ui/cn';
import { isActive, navItems } from './navItems';

/** <1024 px tab bar: the same five destinations as the rail. */
export function BottomTabBar() {
  const pathname = usePathname();

  return (
    <nav aria-label="Main" className="fixed bottom-0 left-0 right-0 z-40 border-t border-line bg-canvas lg:hidden">
      <ul className="grid grid-cols-5">
        {navItems.map((item) => {
          const active = isActive(pathname, item);
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'flex min-h-16 items-center justify-center text-[13px]',
                  active ? 'font-semibold text-ink-1 shadow-[inset_0_3px_0_var(--ink-1)]' : 'text-ink-2 hover:text-ink-1',
                )}
              >
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
