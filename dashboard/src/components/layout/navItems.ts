import {
  CandlestickChart,
  FlaskConical,
  LayoutDashboard,
  Settings,
  ShieldAlert,
  type LucideIcon,
} from 'lucide-react';

export interface NavItem {
  label: string;
  href: string;
  icon: LucideIcon;
  /** Other routes that live under this destination (03 §3). */
  under?: string[];
  children?: Array<{ label: string; href: string }>;
}

/** The five destinations; the rail and the tab bar both render exactly this list. */
export const navItems: NavItem[] = [
  { label: 'Status', href: '/', icon: LayoutDashboard, under: ['/portfolio'] },
  { label: 'Sessions', href: '/trading', icon: CandlestickChart },
  { label: 'Risk', href: '/risk', icon: ShieldAlert },
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
  { label: 'Settings', href: '/settings', icon: Settings },
];

const within = (pathname: string, href: string) =>
  href === '/' ? pathname === '/' : pathname === href || pathname.startsWith(`${href}/`);

export function isActive(pathname: string, item: Pick<NavItem, 'href' | 'under'>): boolean {
  return [item.href, ...(item.under ?? [])].some((href) => within(pathname, href));
}

export const activeItem = (pathname: string): NavItem | undefined =>
  navItems.find((item) => isActive(pathname, item));
