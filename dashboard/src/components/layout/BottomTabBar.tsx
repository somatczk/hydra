'use client';

import { usePathname } from 'next/navigation';
import Link from 'next/link';
import { cn } from '@/components/ui/cn';
import { isActive, navItems } from './navItems';

/** <1024 px tab bar: the five non-railOnly destinations, no More menu. */
export function BottomTabBar() {
  const pathname = usePathname();

  return (
    <nav aria-label="Main" className="fixed bottom-0 left-0 right-0 z-40 border-t border-line bg-canvas lg:hidden">
      <ul className="flex">
        {navItems
          .filter((item) => !item.railOnly)
          .map((item) => {
            const active = isActive(pathname, item);
            return (
              <li key={item.href} className="flex-1">
                <Link
                  href={item.href}
                  aria-current={active ? 'page' : undefined}
                  className={cn(
                    'flex min-h-14 flex-col items-center justify-center gap-0.5 text-xs font-medium',
                    active ? 'text-accent' : 'text-ink-2 hover:text-ink-1',
                  )}
                >
                  <item.icon className="h-5 w-5" aria-hidden="true" />
                  {item.label}
                </Link>
              </li>
            );
          })}
      </ul>
    </nav>
  );
}
