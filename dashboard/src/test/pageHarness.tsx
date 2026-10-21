import { act, type ReactNode } from 'react';
import { vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { ApiError } from '@/lib/api';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

class NoopResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
(globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= NoopResizeObserver;

export interface Mounted {
  container: HTMLDivElement;
  unmount: () => void;
}

export async function mount(node: ReactNode): Promise<Mounted> {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root: Root = createRoot(container);
  await act(async () => {
    root.render(node);
  });
  await flush();
  return {
    container,
    unmount: () => {
      act(() => root.unmount());
      container.remove();
    },
  };
}

export async function flush(rounds = 5): Promise<void> {
  for (let i = 0; i < rounds; i++) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

export function unavailable(message = 'Database is not available'): ApiError {
  return new ApiError(
    503,
    JSON.stringify({ detail: { code: 'database_unavailable', message, state: 'unavailable' } }),
  );
}

export type Routes = Record<string, unknown | (() => unknown)>;

/** Non-GET responses keyed "METHOD /path"; a function gets the 1-based call count for that key. */
export type Mutations = Record<string, unknown | ((call: number) => unknown)>;

/** fetchApi stand-in: GETs resolve from `routes` by path (query included); Errors reject.
 * Non-GETs resolve from `mutations` when scripted, else `{}`. */
export function routeFetch(
  routes: Routes,
  calls: Array<{ path: string; init?: RequestInit }>,
  mutations: Mutations = {},
) {
  const counts = new Map<string, number>();
  return async (path: string, init?: RequestInit): Promise<unknown> => {
    calls.push({ path, init });
    const method = (init?.method ?? 'GET').toUpperCase();
    if (method !== 'GET') {
      const key = `${method} ${path}`;
      if (!(key in mutations)) return {};
      const n = (counts.get(key) ?? 0) + 1;
      counts.set(key, n);
      const entry = mutations[key];
      const value = typeof entry === 'function' ? (entry as (call: number) => unknown)(n) : entry;
      if (value instanceof Error) throw value;
      return value;
    }
    if (!(path in routes)) throw new ApiError(404, JSON.stringify({ detail: 'no route' }));
    const entry = routes[path];
    const value = typeof entry === 'function' ? (entry as () => unknown)() : entry;
    if (value instanceof Error) throw value;
    return value;
  };
}

/** jsdom 29.1.1 has no HTMLDialogElement.showModal/close; this stub toggles `open` and fires `close`. */
export function dialogPolyfill(): void {
  const proto = HTMLDialogElement.prototype;
  if (typeof proto.showModal === 'function') return;
  proto.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  proto.show = proto.showModal;
  proto.close = function (this: HTMLDialogElement, returnValue?: string) {
    if (!this.hasAttribute('open')) return;
    if (returnValue !== undefined) this.returnValue = returnValue;
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
}

/** Advance fake timers inside act, then let promises settle. */
export async function advance(ms: number): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
  await flush();
}

export function inputByLabel(container: HTMLElement, label: string): HTMLInputElement {
  const el = Array.from(container.querySelectorAll('label')).find((l) => l.textContent === label);
  if (!el) throw new Error(`no label ${label}`);
  const input = document.getElementById(el.htmlFor);
  if (!(input instanceof HTMLInputElement)) throw new Error(`no input for ${label}`);
  return input;
}

export async function typeInto(input: HTMLInputElement, value: string): Promise<void> {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  await act(async () => {
    setter?.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

export function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll('button')).find(
    (b) => b.textContent?.trim() === text,
  );
  if (!btn) throw new Error(`no button ${text}`);
  return btn;
}

export async function click(el: HTMLElement): Promise<void> {
  await act(async () => {
    el.click();
  });
  await flush();
}

/** Text of the closest card-like block containing `heading`. */
export function sectionText(container: HTMLElement, heading: string): string {
  const h = Array.from(container.querySelectorAll('h3, p')).find((n) => n.textContent === heading);
  let node: HTMLElement | null = h as HTMLElement | null;
  while (node && !node.className.includes('rounded-')) node = node.parentElement;
  return node?.textContent ?? '';
}
