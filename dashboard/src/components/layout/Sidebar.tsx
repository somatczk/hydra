'use client';

import { usePathname } from 'next/navigation';
import Link from 'next/link';
import {
  LayoutDashboard,
  CandlestickChart,
  Zap,
  Wrench,
  FlaskConical,
  Briefcase,
  ShieldAlert,
  Brain,
  Settings,
  X,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { HydraLogo } from '@/components/icons/HydraLogo';

interface NavItem {
  label: string;
  href: string;
  icon: LucideIcon;
}

export const navItems: NavItem[] = [
  { label: 'Dashboard', href: '/', icon: LayoutDashboard },
  { label: 'Trading', href: '/trading', icon: CandlestickChart },
  { label: 'Strategies', href: '/strategies', icon: Zap },
  { label: 'Strategy Builder', href: '/builder', icon: Wrench },
  { label: 'Backtest', href: '/backtest', icon: FlaskConical },
  { label: 'Portfolio', href: '/portfolio', icon: Briefcase },
  { label: 'Risk', href: '/risk', icon: ShieldAlert },
  { label: 'Models', href: '/models', icon: Brain },
  { label: 'Settings', href: '/settings', icon: Settings },
];

interface SidebarProps {
  open: boolean;
  onClose: () => void;
}

export function Sidebar({ open, onClose }: SidebarProps) {
  const pathname = usePathname();

  const isActive = (href: string) => {
    if (href === '/') return pathname === '/';
    return pathname.startsWith(href);
  };

  return (
    <>
      {/* Overlay on mobile */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/40 lg:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      {/* Sidebar panel */}
      <aside
        className={cn(
          'fixed left-0 top-0 z-50 flex h-full w-72 flex-col border-r border-border-default bg-bg-primary transition-transform duration-200 ease-in-out',
          'lg:translate-x-0',
          open ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        {/* Logo */}
        <div className="flex h-16 items-center justify-between border-b border-border-default px-6">
          <Link
            href="/"
            className="flex items-center gap-2.5"
            onClick={onClose}
          >
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-primary">
              <HydraLogo className="h-5 w-5 text-white" />
            </div>
            <span className="text-lg font-bold tracking-tight text-text-primary font-display">
              Hydra
            </span>
          </Link>
          <button
            type="button"
            className="rounded-lg p-1.5 text-text-muted hover:bg-bg-hover hover:text-text-primary transition-colors lg:hidden"
            onClick={onClose}
            aria-label="Close sidebar"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Navigation */}
        <nav className="flex-1 overflow-y-auto px-3 py-4" aria-label="Main navigation">
          <ul className="flex flex-col gap-1">
            {navItems.map((item) => {
              const active = isActive(item.href);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    onClick={onClose}
                    className={cn(
                      'flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors',
                      active
                        ? 'bg-accent-primary/10 text-accent-primary'
                        : 'text-text-secondary hover:bg-bg-hover hover:text-text-primary',
                    )}
                    aria-current={active ? 'page' : undefined}
                  >
                    <item.icon
                      className={cn(
                        'h-[18px] w-[18px] shrink-0',
                        active ? 'text-accent-primary' : 'text-text-muted',
                      )}
                      aria-hidden="true"
                    />
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        {/* Footer */}
        <div className="border-t border-border-default px-4 py-3">
          <p className="text-xs text-text-light">Hydra v0.1.0</p>
        </div>
      </aside>
    </>
  );
}
