import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { DataStateBadge } from './DataStateBadge';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render(node: React.ReactNode): void {
  act(() => root.render(node));
}

describe('DataStateBadge', () => {
  it('renders UNAVAILABLE with the reason linked by aria-describedby', () => {
    render(<DataStateBadge state="unavailable" reason="not wired to engine yet" />);
    const badge = container.querySelector('[role="status"]');
    expect(badge?.textContent).toBe('UNAVAILABLE');
    expect(badge?.getAttribute('data-state')).toBe('unavailable');
    const describedBy = badge?.getAttribute('aria-describedby') ?? '';
    expect(document.getElementById(describedBy)?.textContent).toBe('not wired to engine yet');
    expect(badge?.querySelector('.text-caution')).not.toBeNull();
  });

  it.each([
    ['stale', 'STALE', '.text-caution'],
    ['unknown', 'UNKNOWN', '.text-caution'],
    ['unauthenticated', 'NOT SIGNED IN', '.text-caution'],
    ['forbidden', 'FORBIDDEN', '.text-caution'],
    ['no_data', 'NO DATA', '.text-ink-2'],
    ['not_found', 'NOT FOUND', '.text-ink-2'],
    ['not_implemented', 'NOT IMPLEMENTED', '.text-ink-2'],
  ] as const)('renders %s as %s', (state, label, tone) => {
    render(<DataStateBadge state={state} />);
    const badge = container.querySelector('[role="status"]');
    expect(badge?.textContent).toBe(label);
    expect(badge?.hasAttribute('aria-describedby')).toBe(false);
    expect(badge?.querySelector(tone)).not.toBeNull();
  });

  it('renders nothing for ok data', () => {
    render(<DataStateBadge state="ok" />);
    expect(container.innerHTML).toBe('');
  });
});
