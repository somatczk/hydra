'use client';

import { usePathname } from 'next/navigation';
import { Menu, Sun, Moon } from 'lucide-react';
import { useTheme } from './ThemeProvider';
import { cn } from '@/components/ui/cn';

const pageTitles: Record<string, string> = {
  '/': 'Dashboard',
  '/trading': 'Trading',
  '/strategies': 'Strategies',
  '/builder': 'Strategy Builder',
  '/backtest': 'Backtest',
  '/portfolio': 'Portfolio',
  '/risk': 'Risk Management',
  '/models': 'ML Models',
  '/settings': 'Settings',
};

interface HeaderProps {
  onMenuClick: () => void;
}

export function Header({ onMenuClick }: HeaderProps) {
  const pathname = usePathname();
  const { resolved, setTheme } = useTheme();

  const title = pageTitles[pathname] ?? 'Hydra';

  const toggleTheme = () => {
    setTheme(resolved === 'dark' ? 'light' : 'dark');
  };

  return (
    <header
      className={cn(
        'sticky top-0 z-30 flex h-16 items-center justify-between border-b border-border-default bg-bg-primary/80 px-4 backdrop-blur-sm',
        'lg:pl-[calc(18rem+1.5rem)]',
      )}
    >
      <div className="flex items-center gap-3">
        <button
          type="button"
          className="rounded-lg p-2 text-text-muted hover:bg-bg-hover hover:text-text-primary transition-colors lg:hidden"
          onClick={onMenuClick}
          aria-label="Open navigation menu"
        >
          <Menu className="h-5 w-5" />
        </button>
        <h1 className="text-lg font-semibold text-text-primary font-display">
          {title}
        </h1>
      </div>

      <button
        type="button"
        onClick={toggleTheme}
        className="rounded-lg p-2 text-text-muted hover:bg-bg-hover hover:text-text-primary transition-colors"
        aria-label={`Switch to ${resolved === 'dark' ? 'light' : 'dark'} mode`}
      >
        {resolved === 'dark' ? (
          <Sun className="h-5 w-5" />
        ) : (
          <Moon className="h-5 w-5" />
        )}
      </button>
    </header>
  );
}
