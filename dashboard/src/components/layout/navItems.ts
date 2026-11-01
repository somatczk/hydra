export interface NavItem {
  label: string;
  href: string;
  /** Other routes that live under this destination. */
  under?: string[];
  /** Shown in the rail while the destination is active, and as the phone sub-nav. */
  children?: Array<{ label: string; href: string }>;
}

/** The five destinations; the rail and the phone tab bar both render all of them. */
export const navItems: NavItem[] = [
  { label: 'Home', href: '/' },
  {
    label: 'Strategies',
    href: '/strategies',
    under: ['/builder', '/templates', '/backtest', '/optimize', '/models', '/trading'],
    children: [
      { label: 'Strategies', href: '/strategies' },
      { label: 'Sessions', href: '/trading' },
      { label: 'Backtests', href: '/backtest' },
      { label: 'Optimize', href: '/optimize' },
      { label: 'Models', href: '/models' },
      { label: 'Templates', href: '/templates' },
    ],
  },
  { label: 'Money', href: '/portfolio' },
  { label: 'Inbox', href: '/activity' },
  {
    label: 'System',
    href: '/settings',
    under: ['/risk', '/data'],
    children: [
      { label: 'Settings', href: '/settings' },
      { label: 'Risk', href: '/risk' },
      { label: 'Data', href: '/data' },
    ],
  },
];

const within = (pathname: string, href: string) =>
  href === '/' ? pathname === '/' : pathname === href || pathname.startsWith(`${href}/`);

export function isActive(pathname: string, item: Pick<NavItem, 'href' | 'under'>): boolean {
  return [item.href, ...(item.under ?? [])].some((href) => within(pathname, href));
}

export const activeItem = (pathname: string): NavItem | undefined =>
  navItems.find((item) => isActive(pathname, item));

/** App-bar title: the active child's label when there is one, else the destination's. */
export function pageTitle(pathname: string): string | undefined {
  const item = activeItem(pathname);
  return item?.children?.find((c) => within(pathname, c.href))?.label ?? item?.label;
}
