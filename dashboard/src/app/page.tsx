import type { Metadata } from 'next';
import { StatusPage } from '@/components/status/StatusPage';

export const metadata: Metadata = { title: 'Home' };

export default function Page() {
  return <StatusPage />;
}
