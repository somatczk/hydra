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
const { AppLayout } = await import('./AppLayout');
const { ThemeProvider } = await import('./ThemeProvider');
const { isActive, navItems, pageTitle } = await import('./navItems');

const LABELS = ['Status', 'Sessions', 'Risk', 'Research', 'Settings'];
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
  it('renders the same five destinations in the same order in the rail and the tab bar', async () => {
    const { rail, tabs } = await bars('/');
    expect(topLabels(rail)).toEqual(LABELS);
    expect(topLabels(tabs)).toEqual(LABELS);
    for (const a of tabs.querySelectorAll('a')) expect(a.className).toContain('min-h-14');
    expect(m.container.textContent).not.toMatch(/More|Models/);
  });

  it.each([
    ['/', 'Status'],
    ['/portfolio', 'Status'],
    ['/trading', 'Sessions'],
    ['/trading/abc', 'Sessions'],
    ['/risk', 'Risk'],
    ['/builder', 'Research'],
    ['/templates', 'Research'],
    ['/settings', 'Settings'],
  ])('marks %s as under %s', async (at, label) => {
    const { rail, tabs } = await bars(at);
    expect(current(tabs)).toEqual([label]);
    expect(current(rail)).toEqual([label]);
  });

  it('shows Research children in the rail only while Research is active, marking the child', async () => {
    let { rail } = await bars('/backtest/run-1');
    expect(current(rail)).toEqual(['Backtests']);
    expect(rail.textContent).toContain('StrategiesBacktestsOptimize');
    m.unmount();
    ({ rail } = await bars('/risk'));
    expect(rail.textContent).not.toContain('Backtests');
  });

  it('matches nothing for routes outside the five destinations', () => {
    expect(navItems.filter((item) => isActive('/models', item))).toEqual([]);
    expect(navItems.filter((item) => isActive('/', item)).map((i) => i.label)).toEqual(['Status']);
    expect(navItems.filter((item) => isActive('/riskier', item))).toEqual([]);
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

  it.each(['/', '/trading', '/trading/abc', '/risk', '/strategies', '/backtest', '/portfolio', '/settings', '/models'])(
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
  it('names /portfolio Performance while it stays under Status, and otherwise uses the destination label', () => {
    expect(pageTitle('/portfolio')).toBe('Performance');
    expect(isActive('/portfolio', navItems[0])).toBe(true);
    expect(pageTitle('/')).toBe('Status');
    expect(pageTitle('/backtest/abc')).toBe('Research');
  });
});
