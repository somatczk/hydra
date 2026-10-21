import { afterEach, describe, expect, it } from 'vitest';
import { mount, type Mounted } from '@/test/pageHarness';
import { Button } from './Button';

let m: Mounted;
afterEach(() => m.unmount());

describe('Button targets (R-P2-8, 08 §3.9)', () => {
  it('sm and md are 44 px (h-11) below 1024 px; lg stays h-12', async () => {
    m = await mount(
      <>
        <Button size="sm">sm</Button>
        <Button size="md">md</Button>
        <Button size="lg">lg</Button>
      </>,
    );
    const [sm, md, lg] = Array.from(m.container.querySelectorAll('button')).map((b) => b.classList);
    expect(sm.contains('max-lg:h-11')).toBe(true);
    expect(md.contains('max-lg:h-11')).toBe(true);
    expect(lg.contains('h-12')).toBe(true);
    expect(lg.contains('max-lg:h-11')).toBe(false);
  });
});
