import type { Metadata } from 'next';
import type { ReactNode } from 'react';

export const metadata: Metadata = { title: 'Risk' };

export default function RiskLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <h1 className="mb-4 text-xl font-semibold text-ink-1">Risk</h1>
      {children}
    </>
  );
}
