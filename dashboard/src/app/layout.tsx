import type { Metadata } from 'next';
import localFont from 'next/font/local';
import { ThemeProvider } from '@/components/layout/ThemeProvider';
import { ToastProvider } from '@/components/ui/Toast';
import { AppLayout } from '@/components/layout/AppLayout';
import './globals.css';

export const metadata: Metadata = {
  title: { template: '%s · Hydra', default: 'Hydra' },
  description:
    'Bitcoin auto-trading platform with ML-assisted signals, multi-exchange support, backtesting, and production monitoring.',
};

// Vendored OFL files (app/fonts): font-src 'self' covers them and the build needs no network.
const publicSans = localFont({
  src: './fonts/public-sans-latin-wght-normal.woff2',
  weight: '100 900',
  variable: '--font-public-sans',
  display: 'swap',
});
const barlowCondensed = localFont({
  src: [
    { path: './fonts/barlow-condensed-latin-500-normal.woff2', weight: '500' },
    { path: './fonts/barlow-condensed-latin-600-normal.woff2', weight: '600' },
  ],
  variable: '--font-barlow-condensed',
  display: 'swap',
});

// Same rule as ThemeProvider (stored choice, else the system's), applied before hydration so nothing flashes.
const THEME_SCRIPT = `try{var t=localStorage.getItem('hydra-theme');document.documentElement.classList.toggle('dark',t==='light'?false:t==='dark'?true:matchMedia('(prefers-color-scheme: dark)').matches)}catch(e){document.documentElement.classList.add('dark')}`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning className={`${publicSans.variable} ${barlowCondensed.variable}`}>
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
