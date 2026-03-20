'use client';

import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Button } from './Button';

interface ErrorCardProps {
  message: string;
  onRetry?: () => void;
}

export function ErrorCard({ message, onRetry }: ErrorCardProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-status-warning/30 bg-status-warning/5 py-10 px-6 text-center">
      <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-status-warning/10">
        <AlertTriangle className="h-5 w-5 text-status-warning" aria-hidden="true" />
      </div>
      <p className="text-sm text-text-primary">{message}</p>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RefreshCw className="h-3.5 w-3.5" />
          Retry
        </Button>
      )}
    </div>
  );
}
