'use client';

import { usePathname } from 'next/navigation';
import Link from 'next/link';
import { cn } from '@/components/ui/cn';
import { isActive, navItems } from './navItems';

/** ≥1024 px rail: the five destinations as text; the active one shows its children. */
export function Sidebar() {
  const pathname = usePathname();

  return (
    <nav
      aria-label="Main"
      className="fixed left-0 top-0 z-40 flex h-full w-56 flex-col border-r border-line bg-canvas max-lg:hidden"
    >
      <Link href="/" className="flex h-16 items-center px-6 font-display text-2xl font-semibold text-ink-1">
        Hydra
      </Link>
      <ul className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-6 py-2">
        {navItems.map((item) => {
          const active = isActive(pathname, item);
          const childActive = item.children?.some((c) => isActive(pathname, c)) ?? false;
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active && !childActive ? 'page' : undefined}
                className={cn(
                  '-ml-3 flex h-10 items-center pl-3 text-[15px]',
                  active ? 'font-semibold text-ink-1 shadow-[inset_3px_0_0_var(--ink-1)]' : 'text-ink-2 hover:text-ink-1',
                )}
              >
                {item.label}
              </Link>
              {active && item.children && (
                <ul className="mb-2 flex flex-col">
                  {item.children.map((child) => {
                    const here = isActive(pathname, child);
                    return (
                      <li key={child.href}>
                        <Link
                          href={child.href}
                          aria-current={here ? 'page' : undefined}
                          className={cn(
                            'flex h-8 items-center pl-3 text-sm',
                            here ? 'font-semibold text-ink-1 underline decoration-2 underline-offset-4' : 'text-ink-2 hover:text-ink-1',
                          )}
                        >
                          {child.label}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
      <p className="px-6 py-3 text-xs text-ink-2">Hydra v{process.env.APP_VERSION}</p>
    </nav>
  );
}
