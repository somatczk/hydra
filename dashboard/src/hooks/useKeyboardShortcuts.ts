/**
 * Global keyboard shortcuts for the Hydra dashboard.
 *
 * K - Toggle kill switch (with confirmation)
 * 1-9 - Navigate to pages
 * N - New strategy (navigate to builder)
 * ? - Show shortcut help (TODO: overlay component)
 */
import { useEffect, useCallback } from 'react';
import { useRouter } from 'next/navigation';

const PAGE_MAP: Record<string, string> = {
  '1': '/',
  '2': '/strategies',
  '3': '/builder',
  '4': '/trading',
  '5': '/backtest',
  '6': '/models',
  '7': '/portfolio',
  '8': '/risk',
  '9': '/settings',
};

interface UseKeyboardShortcutsOptions {
  onKillSwitch?: () => void;
}

export function useKeyboardShortcuts(options: UseKeyboardShortcutsOptions = {}) {
  const router = useRouter();

  const handler = useCallback(
    (e: KeyboardEvent) => {
      // Ignore when typing in inputs/textareas
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;

      const key = e.key.toLowerCase();

      // Page navigation (1-9)
      if (key in PAGE_MAP) {
        e.preventDefault();
        router.push(PAGE_MAP[key]);
        return;
      }

      // N = New strategy
      if (key === 'n') {
        e.preventDefault();
        router.push('/builder');
        return;
      }

      // K = Kill switch toggle
      if (key === 'k' && options.onKillSwitch) {
        e.preventDefault();
        options.onKillSwitch();
        return;
      }
    },
    [router, options],
  );

  useEffect(() => {
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [handler]);
}
