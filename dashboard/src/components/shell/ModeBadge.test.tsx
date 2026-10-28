import { afterEach, describe, expect, it } from 'vitest';
import { mount, type Mounted } from '@/test/pageHarness';

const { ModeBadge } = await import('./ModeBadge');

let m: Mounted;

afterEach(() => {
  m.unmount();
});

const badge = () => m.container.firstChild as HTMLElement;

describe('ModeBadge', () => {
  it('paper is the outlined PAPER chip with its screen-reader truth, and no real-money claim', async () => {
    m = await mount(<ModeBadge mode="paper" />);
    expect(badge().textContent).toBe('PAPERPaper trading: no real money');
    expect(m.container.querySelector('span')?.className).toContain('border-ink-2');
    expect(badge().textContent).not.toContain('Live trading: real money');
    expect(m.container.innerHTML).not.toContain('bg-live');
  });

  it('live is the filled LIVE chip on the live tokens, with visible and spoken real money', async () => {
    m = await mount(<ModeBadge mode="live" />);
    expect(badge().textContent).toBe('LIVELive trading: real moneyreal money');
    const chip = m.container.querySelector('span > span');
    expect(chip?.className).toContain('bg-live');
    expect(chip?.className).toContain('text-on-live');
    expect(m.container.innerHTML).not.toMatch(/danger/);
  });

  it('an unreadable mode renders "?", never PAPER (R23)', async () => {
    m = await mount(<ModeBadge mode={null} />);
    expect(badge().textContent).toBe('?Trading mode unknown');
    expect(badge().textContent).not.toContain('PAPER');
    expect(badge().textContent).not.toContain('real money');
  });

  it('carries no stored-mode caution anywhere (the R-P2-9 caution is deleted)', async () => {
    m = await mount(<ModeBadge mode="paper" />);
    expect(badge().textContent).not.toContain('Stored mode');
  });
});
