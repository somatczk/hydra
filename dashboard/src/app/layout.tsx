import type { Metadata } from 'next';
import { ThemeProvider } from '@/components/layout/ThemeProvider';
import { ToastProvider } from '@/components/ui/Toast';
import { AppLayout } from '@/components/layout/AppLayout';
import './globals.css';

export const metadata: Metadata = {
  title: { template: '%s · Hydra', default: 'Hydra' },
  description:
    'Bitcoin auto-trading platform with ML-assisted signals, multi-exchange support, backtesting, and production monitoring.',
};

// Same rule as ThemeProvider (stored choice, dark by default), applied before hydration so nothing flashes.
const THEME_SCRIPT = `try{var t=localStorage.getItem('hydra-theme');document.documentElement.classList.toggle('dark',t==='light'?false:t==='system'?matchMedia('(prefers-color-scheme: dark)').matches:true)}catch(e){document.documentElement.classList.add('dark')}`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
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
