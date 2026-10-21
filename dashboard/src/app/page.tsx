import type { Metadata } from 'next';
import { StatusPage } from '@/components/status/StatusPage';

export const metadata: Metadata = { title: 'Status' };

export default function Page() {
  return <StatusPage />;
}
