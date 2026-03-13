'use client';

import { useState } from 'react';
import { usePathname } from 'next/navigation';
import Link from 'next/link';
import {
  LayoutDashboard,
  CandlestickChart,
  Zap,
  Briefcase,
  MoreHorizontal,
  X,
  Wrench,
  FlaskConical,
  ShieldAlert,
  Brain,
  Settings,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';

interface TabItem {
  label: string;
  href: string;
  icon: LucideIcon;
}

const primaryTabs: TabItem[] = [
  { label: 'Dashboard', href: '/', icon: LayoutDashboard },
  { label: 'Trading', href: '/trading', icon: CandlestickChart },
  { label: 'Strategies', href: '/strategies', icon: Zap },
  { label: 'Portfolio', href: '/portfolio', icon: Briefcase },
];

const moreItems: TabItem[] = [
  { label: 'Strategy Builder', href: '/builder', icon: Wrench },
  { label: 'Backtest', href: '/backtest', icon: FlaskConical },
  { label: 'Risk', href: '/risk', icon: ShieldAlert },
  { label: 'Models', href: '/models', icon: Brain },
  { label: 'Settings', href: '/settings', icon: Settings },
];

export function BottomTabBar() {
  const pathname = usePathname();
  const [moreOpen, setMoreOpen] = useState(false);

  const isActive = (href: string) => {
    if (href === '/') return pathname === '/';
    return pathname.startsWith(href);
  };

  const isMoreActive = moreItems.some((item) => isActive(item.href));

  return (
    <>
      {/* More menu overlay */}
      {moreOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/40 md:hidden"
          onClick={() => setMoreOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* More menu panel */}
      {moreOpen && (
        <div className="fixed bottom-16 left-0 right-0 z-50 mx-4 mb-2 rounded-xl border border-border-default bg-bg-elevated shadow-lg md:hidden">
          <div className="flex items-center justify-between border-b border-border-default px-4 py-3">
            <span className="text-sm font-semibold text-text-primary">More</span>
            <button
              type="button"
              onClick={() => setMoreOpen(false)}
              className="rounded-lg p-1 text-text-muted hover:text-text-primary transition-colors"
              aria-label="Close menu"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <nav aria-label="Additional navigation">
            <ul className="py-2">
              {moreItems.map((item) => {
                const active = isActive(item.href);
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      onClick={() => setMoreOpen(false)}
                      className={cn(
                        'flex items-center gap-3 px-4 py-2.5 text-sm font-medium transition-colors',
                        active
                          ? 'text-accent-primary'
                          : 'text-text-secondary hover:bg-bg-hover',
                      )}
                      aria-current={active ? 'page' : undefined}
                    >
                      <item.icon className="h-4 w-4" aria-hidden="true" />
                      {item.label}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </nav>
        </div>
      )}

      {/* Tab bar */}
      <nav
        className="fixed bottom-0 left-0 right-0 z-40 border-t border-border-default bg-bg-primary md:hidden"
        aria-label="Bottom navigation"
      >
        <div className="flex items-center justify-around">
          {primaryTabs.map((tab) => {
            const active = isActive(tab.href);
            return (
              <Link
                key={tab.href}
                href={tab.href}
                className={cn(
                  'flex flex-1 flex-col items-center gap-0.5 py-2 text-xs font-medium transition-colors',
                  active
                    ? 'text-accent-primary'
                    : 'text-text-muted hover:text-text-primary',
                )}
                aria-current={active ? 'page' : undefined}
              >
                <tab.icon className="h-5 w-5" aria-hidden="true" />
                <span>{tab.label}</span>
              </Link>
            );
          })}
          <button
            type="button"
            onClick={() => setMoreOpen((prev) => !prev)}
            className={cn(
              'flex flex-1 flex-col items-center gap-0.5 py-2 text-xs font-medium transition-colors',
              isMoreActive || moreOpen
                ? 'text-accent-primary'
                : 'text-text-muted hover:text-text-primary',
            )}
            aria-label="More navigation options"
            aria-expanded={moreOpen}
          >
            <MoreHorizontal className="h-5 w-5" aria-hidden="true" />
            <span>More</span>
          </button>
        </div>
      </nav>
    </>
  );
}
