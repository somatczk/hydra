'use client';

import type { ReactNode } from 'react';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
import { BottomTabBar } from './BottomTabBar';
import { KillSwitchOutcome } from '@/components/shell/KillSwitchControl';
import { ShellStatusProvider } from '@/components/shell/ShellStatusProvider';

/** DOM order is the Tab order: skip link → kill-switch control (in the header) → nav → main. */
export function AppLayout({ children }: { children: ReactNode }) {
  return (
    <ShellStatusProvider>
      <div className="min-h-screen bg-canvas">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-4 focus:py-2 focus:text-ink-1 focus:outline-2 focus:outline-accent"
        >
          Skip to content
        </a>
        <Header />
        <Sidebar />
        <BottomTabBar />
        <main id="main" tabIndex={-1} className="px-4 py-6 pb-24 outline-none lg:pb-6 lg:pl-62 lg:pr-6">
          <KillSwitchOutcome />
          {children}
        </main>
      </div>
    </ShellStatusProvider>
  );
}
