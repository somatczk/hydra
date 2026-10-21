import axe from 'axe-core';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { dialogPolyfill, mount, unavailable, type Mounted } from '@/test/pageHarness';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const { ThemeProvider } = await import('@/components/layout/ThemeProvider');
const pages: Array<[string, () => Promise<{ default: () => ReactNode }>]> = [
  ['/portfolio', () => import('@/app/portfolio/page')],
  ['/settings', () => import('@/app/settings/page')],
  ['/strategies', () => import('@/app/strategies/page')],
  ['/backtest', () => import('@/app/backtest/page')],
  ['/optimize', () => import('@/app/optimize/page')],
  ['/models', () => import('@/app/models/page')],
  ['/builder', () => import('@/app/builder/page')],
];

let page: Mounted | null = null;
afterEach(() => {
  page?.unmount();
  page = null;
});

describe('axe (jsdom: structure, names and roles; contrast and layout are checked by hand)', () => {
  it.each(pages)('%s has no violations when its data is unavailable', async (_route, load) => {
    dialogPolyfill();
    fetchApi.mockReset();
    fetchApi.mockRejectedValue(unavailable());
    const { default: Page } = await load();
    page = await mount(
      <ThemeProvider>
        <main>
          <Page />
        </main>
      </ThemeProvider>,
    );

    const results = await axe.run(page.container, {
      rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
    });
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(' | ')}`)).toEqual([]);
  });
});
