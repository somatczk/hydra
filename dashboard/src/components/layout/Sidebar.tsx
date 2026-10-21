'use client';

import { usePathname } from 'next/navigation';
import Link from 'next/link';
import { cn } from '@/components/ui/cn';
import { HydraLogo } from '@/components/icons/HydraLogo';
import { isActive, navItems } from './navItems';

/** ≥1024 px rail: the five destinations; Research shows its children while active. */
export function Sidebar() {
  const pathname = usePathname();

  return (
    <nav
      aria-label="Main"
      className="fixed left-0 top-0 z-40 flex h-full w-56 flex-col border-r border-line bg-canvas max-lg:hidden"
    >
      <Link href="/" className="flex h-14 items-center gap-2.5 px-5">
        <span className="flex h-7 w-7 items-center justify-center rounded bg-accent">
          <HydraLogo className="h-4 w-4 text-on-accent" />
        </span>
        <span className="text-base font-semibold text-ink-1">Hydra</span>
      </Link>
      <ul className="flex flex-1 flex-col gap-1 overflow-y-auto px-3 py-2">
        {navItems.map((item) => {
          const active = isActive(pathname, item);
          const childActive = item.children?.some((c) => isActive(pathname, c)) ?? false;
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active && !childActive ? 'page' : undefined}
                className={cn(
                  'flex h-9 items-center gap-3 rounded px-3 text-sm font-medium',
                  active ? 'bg-hover text-ink-1' : 'text-ink-2 hover:bg-hover hover:text-ink-1',
                )}
              >
                <item.icon className="h-[18px] w-[18px] shrink-0" aria-hidden="true" />
                {item.label}
              </Link>
              {active && item.children && (
                <ul className="mt-1 flex flex-col gap-0.5 pl-9">
                  {item.children.map((child) => {
                    const here = isActive(pathname, child);
                    return (
                      <li key={child.href}>
                        <Link
                          href={child.href}
                          aria-current={here ? 'page' : undefined}
                          className={cn(
                            'flex h-8 items-center rounded px-2 text-sm',
                            here ? 'font-medium text-ink-1' : 'text-ink-2 hover:text-ink-1',
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
      <p className="border-t border-line px-5 py-3 text-xs text-ink-2">Hydra v{process.env.APP_VERSION}</p>
    </nav>
  );
}
