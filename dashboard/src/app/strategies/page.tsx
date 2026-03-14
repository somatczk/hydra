'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Zap, Plus, Edit2, Trash2, BarChart3 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { cn } from '@/components/ui/cn';
import { fetchApi } from '@/lib/api';

/* ---------- Types ---------- */

interface Strategy {
  id: string;
  name: string;
  description: string;
  status: 'Active' | 'Draft';
  statusVariant: 'success' | 'neutral';
  enabled: boolean;
  editable: boolean;
}

interface BuilderStrategy {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  filename: string;
  editable: boolean;
}

/* ---------- Helpers ---------- */

function mapBuilderStrategies(data: BuilderStrategy[]): Strategy[] {
  return data.map((s) => ({
    id: s.id,
    name: s.name,
    description: s.description || 'Rule-based strategy',
    status: s.enabled ? 'Active' : 'Draft',
    statusVariant: s.enabled ? 'success' : 'neutral',
    enabled: s.enabled,
    editable: s.editable,
  }));
}

/* ---------- Page ---------- */

export default function StrategiesPage() {
  const router = useRouter();
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(true);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const fetchStrategies = async () => {
    setLoading(true);
    try {
      const data = await fetchApi<BuilderStrategy[]>('/api/builder/strategies');
      setStrategies(mapBuilderStrategies(data));
    } catch {
      /* keep empty */
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchStrategies(); }, []);

  const handleToggle = async (id: string) => {
    try {
      await fetchApi(`/api/strategies/${id}/toggle`, { method: 'POST' });
      fetchStrategies();
    } catch {
      /* toggle failed */
    }
  };

  const handleDelete = async (id: string) => {
    setDeletingId(id);
    try {
      await fetchApi(`/api/builder/strategies/${id}`, { method: 'DELETE' });
      fetchStrategies();
    } catch {
      /* delete failed */
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-text-muted">
            {loading ? 'Loading...' : `${strategies.filter((s) => s.status === 'Active').length} active strategies`}
          </p>
        </div>
        <Button variant="primary" onClick={() => router.push('/builder')}>
          <Plus className="h-4 w-4" />
          New Strategy
        </Button>
      </div>

      {/* Strategy cards grid */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {strategies.map((strategy) => (
          <div
            key={strategy.id}
            className="rounded-xl border border-border-default bg-bg-elevated p-5 transition-colors hover:border-border-hover"
          >
            {/* Card header */}
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-bg-tertiary">
                  <Zap className="h-5 w-5 text-accent-primary" aria-hidden="true" />
                </div>
                <div>
                  <h3 className="text-sm font-semibold text-text-primary">
                    {strategy.name}
                  </h3>
                  <p className="mt-0.5 text-xs text-text-muted line-clamp-1">
                    {strategy.description}
                  </p>
                </div>
              </div>
              <StatusBadge
                status={strategy.status}
                variant={strategy.statusVariant}
                size="sm"
              />
            </div>

            {/* Action buttons */}
            <div className="mt-4 flex items-center justify-between border-t border-border-default pt-3">
              <div className="flex items-center gap-2">
                {strategy.editable && (
                  <button
                    onClick={() => router.push(`/builder?strategy=${strategy.id}`)}
                    className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-text-muted hover:bg-bg-hover hover:text-text-primary transition-colors"
                  >
                    <Edit2 className="h-3 w-3" />
                    Edit
                  </button>
                )}
                <button
                  onClick={() => router.push(`/backtest?strategy=${strategy.id}`)}
                  className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-text-muted hover:bg-bg-hover hover:text-text-primary transition-colors"
                >
                  <BarChart3 className="h-3 w-3" />
                  Backtest
                </button>
                <button
                  onClick={() => handleDelete(strategy.id)}
                  disabled={deletingId === strategy.id}
                  className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-text-muted hover:bg-status-error/10 hover:text-status-error transition-colors disabled:opacity-50"
                >
                  <Trash2 className="h-3 w-3" />
                  {deletingId === strategy.id ? 'Deleting...' : 'Delete'}
                </button>
              </div>
              <div
                className={cn(
                  'relative h-6 w-11 cursor-pointer rounded-full transition-colors',
                  strategy.status === 'Active'
                    ? 'bg-status-success'
                    : 'bg-bg-active',
                )}
                role="switch"
                aria-checked={strategy.status === 'Active'}
                aria-label={`${strategy.status === 'Active' ? 'Disable' : 'Enable'} ${strategy.name}`}
                tabIndex={0}
                onClick={() => handleToggle(strategy.id)}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') handleToggle(strategy.id); }}
              >
                <div
                  className={cn(
                    'absolute top-1 h-4 w-4 rounded-full bg-white shadow transition-transform',
                    strategy.status === 'Active'
                      ? 'translate-x-[22px]'
                      : 'translate-x-1',
                  )}
                />
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
