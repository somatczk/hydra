import type { Metadata } from 'next';
import type { ReactNode } from 'react';

export const metadata: Metadata = { title: 'Activity' };

export default function ActivityLayout({ children }: { children: ReactNode }) {
  return children;
}
