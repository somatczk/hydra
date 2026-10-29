import {
  CandlestickChart,
  Database,
  FlaskConical,
  History,
  LayoutDashboard,
  Settings,
  ShieldAlert,
  type LucideIcon,
} from 'lucide-react';

export interface NavItem {
  label: string;
  href: string;
  icon: LucideIcon;
  /** Rail-only (03 §3): the phone tab bar slices these out, so the rail can hold more than five destinations. */
  railOnly?: boolean;
  /** Other routes that live under this destination (03 §3). */
  under?: string[];
  children?: Array<{ label: string; href: string }>;
}

/** The destinations; the rail renders all of them, the tab bar only the non-railOnly ones. */
export const navItems: NavItem[] = [
  { label: 'Status', href: '/', icon: LayoutDashboard, under: ['/portfolio'] },
  { label: 'Sessions', href: '/trading', icon: CandlestickChart },
  { label: 'Risk', href: '/risk', icon: ShieldAlert },
  { label: 'Activity', href: '/activity', icon: History, railOnly: true },
  {
    label: 'Research',
    href: '/strategies',
    icon: FlaskConical,
    under: ['/builder', '/templates', '/backtest', '/optimize'],
    children: [
      { label: 'Strategies', href: '/strategies' },
      { label: 'Backtests', href: '/backtest' },
      { label: 'Optimize', href: '/optimize' },
    ],
  },
  { label: 'Data', href: '/data', icon: Database, railOnly: true },
  { label: 'Settings', href: '/settings', icon: Settings },
];

const within = (pathname: string, href: string) =>
  href === '/' ? pathname === '/' : pathname === href || pathname.startsWith(`${href}/`);

export function isActive(pathname: string, item: Pick<NavItem, 'href' | 'under'>): boolean {
  return [item.href, ...(item.under ?? [])].some((href) => within(pathname, href));
}

export const activeItem = (pathname: string): NavItem | undefined =>
  navItems.find((item) => isActive(pathname, item));

/** App-bar title: /portfolio sits under Status but is named Performance. */
export const pageTitle = (pathname: string): string | undefined =>
  within(pathname, '/portfolio') ? 'Performance' : activeItem(pathname)?.label;
