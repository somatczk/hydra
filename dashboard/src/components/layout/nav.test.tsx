import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mount, routeFetch, type Mounted } from '@/test/pageHarness';
import { liveStatus, platformConfig, session } from '@/test/fixtures';

let pathname = '/';
vi.mock('next/navigation', () => ({ usePathname: () => pathname }));
const fetchApi = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));

const { Sidebar } = await import('./Sidebar');
const { BottomTabBar } = await import('./BottomTabBar');
const { SubNav } = await import('./SubNav');
const { AppLayout } = await import('./AppLayout');
const { ThemeProvider } = await import('./ThemeProvider');
const { isActive, navItems, pageTitle } = await import('./navItems');

const DESTINATIONS = ['Home', 'Strategies', 'Money', 'Inbox', 'System'];
const SRC = path.join(process.cwd(), 'src');

let m: Mounted;
afterEach(() => {
  m?.unmount();
  fetchApi.mockReset();
});

const topLabels = (nav: Element) => Array.from(nav.querySelectorAll(':scope > ul > li > a')).map((a) => a.textContent);
const current = (nav: Element) => Array.from(nav.querySelectorAll('[aria-current="page"]')).map((a) => a.textContent);

async function bars(at: string) {
  pathname = at;
  m = await mount(
    <>
      <Sidebar />
      <BottomTabBar />
    </>,
  );
  const [rail, tabs] = Array.from(m.container.querySelectorAll('nav[aria-label="Main"]'));
  return { rail, tabs };
}

describe('navigation', () => {
  it('renders the same five destinations in the rail and the tab bar, so nothing is unreachable on a phone', async () => {
    const { rail, tabs } = await bars('/');
    expect(topLabels(rail)).toEqual(DESTINATIONS);
    expect(topLabels(tabs)).toEqual(DESTINATIONS);
    for (const a of tabs.querySelectorAll('a')) expect(a.className).toContain('min-h-16');
  });

  it.each([
    ['/', 'Home', 'Home'],
    ['/portfolio', 'Money', 'Money'],
    ['/activity', 'Inbox', 'Inbox'],
    ['/activity/approvals/abc', 'Inbox', 'Inbox'],
    ['/builder', 'Strategies', 'Strategies'],
    ['/strategies', 'Strategies', 'Strategies'],
    ['/trading/abc', 'Sessions', 'Strategies'],
    ['/models', 'Models', 'Strategies'],
    ['/settings', 'Settings', 'System'],
    ['/risk', 'Risk', 'System'],
    ['/data', 'Data', 'System'],
  ])('marks %s as %s in the rail and %s in the tab bar', async (at, inRail, inTabs) => {
    const { rail, tabs } = await bars(at);
    expect(current(rail)).toEqual([inRail]);
    expect(current(tabs)).toEqual([inTabs]);
  });

  it('shows a destination\'s children in the rail only while it is active, marking the child', async () => {
    let { rail } = await bars('/backtest/run-1');
    expect(current(rail)).toEqual(['Backtests']);
    expect(rail.textContent).toContain('StrategiesSessionsBacktestsOptimizeModelsTemplates');
    m.unmount();
    ({ rail } = await bars('/risk'));
    expect(rail.textContent).not.toContain('Backtests');
    expect(rail.textContent).toContain('SettingsRiskData');
  });

  it('gives the phone the active destination\'s children as a sub-nav, and none where there are no children', async () => {
    pathname = '/optimize/task-1';
    m = await mount(<SubNav />);
    const sub = m.container.querySelector('nav[aria-label="Strategies"]');
    expect(sub?.className).toContain('lg:hidden');
    expect(current(sub as Element)).toEqual(['Optimize']);
    m.unmount();
    pathname = '/data';
    m = await mount(<SubNav />);
    expect(current(m.container.querySelector('nav[aria-label="System"]') as Element)).toEqual(['Data']);
    m.unmount();
    pathname = '/';
    m = await mount(<SubNav />);
    expect(m.container.querySelector('nav')).toBeNull();
  });

  it('matches nothing for routes outside the destinations', () => {
    expect(navItems.filter((item) => isActive('/', item)).map((i) => i.label)).toEqual(['Home']);
    expect(navItems.filter((item) => isActive('/riskier', item))).toEqual([]);
    expect(navItems.filter((item) => isActive('/portfolios', item))).toEqual([]);
  });

  it('has no single-key shortcuts left (WCAG 2.1.4)', () => {
    expect(existsSync(path.join(SRC, 'hooks/useKeyboardShortcuts.ts'))).toBe(false);
    const files = (dir: string): string[] =>
      readdirSync(dir).flatMap((f) => {
        const p = path.join(dir, f);
        return statSync(p).isDirectory() ? files(p) : /\.tsx?$/.test(f) ? [p] : [];
      });
    const offenders = files(SRC).filter((f) => !f.endsWith('nav.test.tsx') && readFileSync(f, 'utf8').includes('useKeyboardShortcuts'));
    expect(offenders).toEqual([]);
  });
});

describe('shell tab order', () => {
  const focusables = () =>
    Array.from(m.container.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, [tabindex]')).filter(
      (el) => el.tabIndex >= 0 && !el.closest('dialog:not([open])') && !(el as HTMLButtonElement).disabled,
    );

  it.each(['/', '/trading', '/trading/abc', '/risk', '/activity', '/strategies', '/backtest', '/portfolio', '/settings', '/models', '/data'])(
    'reaches the kill-switch control second on %s, before the nav and main',
    async (at) => {
      pathname = at;
      fetchApi.mockImplementation(
        routeFetch(
          {
            '/api/risk/live-status': liveStatus(),
            '/api/trading/sessions': [session()],
            '/api/system/config': platformConfig(),
          },
          [],
        ),
      );
      m = await mount(
        <ThemeProvider>
          <AppLayout>
            <h1>Page</h1>
            <button type="button">page action</button>
          </AppLayout>
        </ThemeProvider>,
      );
      const order = focusables();
      expect(order[0].textContent).toBe('Skip to content');
      expect(order[0].getAttribute('href')).toBe('#main');
      expect(order[1].getAttribute('aria-label')).toBe('Stop all trading');
      const firstNav = order.findIndex((el) => el.closest('nav'));
      const firstMain = order.findIndex((el) => el.closest('main'));
      expect(firstNav).toBeGreaterThan(1);
      expect(firstMain).toBeGreaterThan(firstNav);
      expect(m.container.querySelector('header')).not.toBeNull();
      expect(m.container.querySelector('main#main')).not.toBeNull();
    },
  );
});

describe('pageTitle', () => {
  it('uses the active child when there is one, else the destination', () => {
    expect(pageTitle('/')).toBe('Home');
    expect(pageTitle('/portfolio')).toBe('Money');
    expect(pageTitle('/backtest/abc')).toBe('Backtests');
    expect(pageTitle('/builder')).toBe('Strategies');
    expect(pageTitle('/activity/approvals/abc')).toBe('Inbox');
    expect(pageTitle('/risk')).toBe('Risk');
  });
});
