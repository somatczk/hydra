import type { Metadata } from 'next';
import { ThemeProvider } from '@/components/layout/ThemeProvider';
import { ToastProvider } from '@/components/ui/Toast';
import { AppLayout } from '@/components/layout/AppLayout';
import './globals.css';

export const metadata: Metadata = {
  title: 'Hydra Trading Platform',
  description:
    'Bitcoin auto-trading platform with ML-assisted signals, multi-exchange support, backtesting, and production monitoring.',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="antialiased">
        <ThemeProvider>
          <ToastProvider>
            <AppLayout>{children}</AppLayout>
          </ToastProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
