import type { Metadata } from 'next';
import type { ReactNode } from 'react';

export const metadata: Metadata = { title: 'Sessions' };

export default function TradingLayout({ children }: { children: ReactNode }) {
  return children;
}
