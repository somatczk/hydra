import { afterEach, describe, expect, it } from 'vitest';
import { mount, type Mounted } from '@/test/pageHarness';
import { ResearchNav } from './ResearchNav';

let page: Mounted | null = null;
afterEach(() => {
  page?.unmount();
  page = null;
});

describe('ResearchNav', () => {
  it('links the three Research pages and marks only the current one', async () => {
    page = await mount(<ResearchNav current="/backtest" />);

    const nav = page.container.querySelector('nav[aria-label="Research"]');
    const links = Array.from(nav?.querySelectorAll('a') ?? []);
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['Strategies', '/strategies'],
      ['Backtests', '/backtest'],
      ['Optimize', '/optimize'],
    ]);
    expect(links.filter((a) => a.getAttribute('aria-current') === 'page').map((a) => a.textContent)).toEqual([
      'Backtests',
    ]);
    expect(links.every((a) => a.className.includes('h-11'))).toBe(true);
  });
});
