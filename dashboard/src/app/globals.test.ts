import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const here = join(process.cwd(), 'src/app');
const css = readFileSync(join(here, 'globals.css'), 'utf8');

/** Custom properties of each top-level `selector { … }` block, in file order. */
function blocks(selector: string): Array<Record<string, string>> {
  const re = new RegExp(`(?:^|\\n)${selector.replace('.', '\\.')}\\s*\\{([^}]*)\\}`, 'g');
  return [...css.matchAll(re)].map((m) =>
    Object.fromEntries([...m[1].matchAll(/--([\w-]+):\s*([^;]+);/g)].map((x) => [x[1], x[2].trim()])),
  );
}

/** WCAG 2.x relative luminance and contrast ratio. */
function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const [light, aliases] = blocks(':root');
const THEMES = { light, dark: blocks('.dark')[0] };

/** 05 §2.1 claims: text roles ≥ 4.5:1 (1.4.3), control outlines ≥ 3:1 (1.4.11). */
const PAIRS: Array<[fg: string, bg: string, min: number]> = [
  ...['ink-1', 'ink-2', 'accent', 'caution', 'danger'].flatMap((fg) =>
    ['canvas', 'surface', 'raised'].map((bg): [string, string, number] => [fg, bg, 4.5]),
  ),
  ['ink-1', 'hover', 4.5],
  ['ink-2', 'hover', 4.5],
  ['on-accent', 'accent', 4.5],
  ['caution', 'caution-soft', 4.5],
  ['danger', 'danger-soft', 4.5],
  ['line-strong', 'surface', 3],
  ['line-strong', 'canvas', 3],
];

describe('design tokens', () => {
  it.each(Object.entries(THEMES).flatMap(([theme, t]) => PAIRS.map(([fg, bg, min]) => [theme, fg, bg, min, t] as const)))(
    '%s: %s on %s ≥ %s:1',
    (_theme, fg, bg, min, t) => {
      expect(t[fg]).toMatch(/^#[0-9a-f]{6}$/);
      expect(contrast(t[fg], t[bg])).toBeGreaterThanOrEqual(min);
    },
  );

  it('defines every role token in both themes and in @theme', () => {
    expect(Object.keys(THEMES.dark).sort()).toEqual(Object.keys(THEMES.light).sort());
    for (const name of Object.keys(THEMES.light)) {
      expect(css).toContain(`--color-${name}: var(--${name});`);
    }
  });

  it('every old-token utility used in src still resolves through an alias', () => {
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const f of readdirSync(dir)) {
        const p = join(dir, f);
        if (statSync(p).isDirectory()) walk(p);
        else if (/\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f)) files.push(p);
      }
    };
    walk(join(here, '..'));
    const used = new Set<string>();
    const re = /[a-z]+(?:-[lrtbxy])?-((?:bg|text|border|status|accent)-[a-z]+(?:-[a-z]+)?)(?=[\s'"`/]|$)/g;
    for (const f of files) for (const m of readFileSync(f, 'utf8').matchAll(re)) used.add(m[1]);
    expect(used.has('text-muted')).toBe(true);
    for (const name of used) {
      expect(css, name).toContain(`--color-${name}: var(--${name});`);
      expect(aliases[name], name).toMatch(/^var\(--[a-z0-9-]+\)$/);
    }
    expect(css).toContain('--font-display: var(--font-sans);');
  });
});
