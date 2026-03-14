'use client';

import { useState, useEffect, useCallback } from 'react';
import { Edit2, Trash2, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import type { StrategySummary, StrategyDetail } from './types';

interface SavedStrategiesProps {
  refreshKey: number;
  onEdit: (detail: StrategyDetail) => void;
  onDelete: () => void;
}

export function SavedStrategies({ refreshKey, onEdit, onDelete }: SavedStrategiesProps) {
  const [strategies, setStrategies] = useState<StrategySummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);

  const fetchStrategies = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/builder/strategies');
      if (!res.ok) throw new Error('Failed to load strategies');
      const data = await res.json();
      setStrategies(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load strategies');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStrategies();
  }, [fetchStrategies, refreshKey]);

  const handleEdit = async (id: string) => {
    setLoadingId(id);
    try {
      const res = await fetch(`/api/builder/strategies/${id}`);
      if (!res.ok) throw new Error('Failed to load strategy');
      const detail: StrategyDetail = await res.json();
      onEdit(detail);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load strategy');
    } finally {
      setLoadingId(null);
    }
  };

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete strategy "${name}"? This cannot be undone.`)) return;
    setDeletingId(id);
    try {
      const res = await fetch(`/api/builder/strategies/${id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('Failed to delete strategy');
      onDelete();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete');
    } finally {
      setDeletingId(null);
    }
  };

  if (loading) {
    return (
      <div className="rounded-xl border border-border-default bg-bg-elevated p-6">
        <h3 className="text-lg font-semibold text-text-primary mb-4">Saved Strategies</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="rounded-lg border border-border-default bg-bg-surface p-4 animate-pulse"
            >
              <div className="h-5 w-3/4 rounded bg-bg-hover mb-2" />
              <div className="h-4 w-full rounded bg-bg-hover mb-3" />
              <div className="h-8 w-1/3 rounded bg-bg-hover" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (strategies.length === 0 && !error) {
    return (
      <div className="rounded-xl border border-border-default bg-bg-elevated p-6">
        <h3 className="text-lg font-semibold text-text-primary mb-2">Saved Strategies</h3>
        <p className="text-sm text-text-muted">
          No saved strategies yet. Build one below and save it to get started.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-border-default bg-bg-elevated p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-text-primary">Saved Strategies</h3>
        <Button variant="ghost" size="sm" onClick={fetchStrategies}>
          <RefreshCw className="h-4 w-4" />
        </Button>
      </div>

      {error && (
        <div className="rounded-lg border border-status-error/30 bg-status-error/5 p-3 mb-4">
          <p className="text-sm text-status-error">{error}</p>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {strategies.map((s) => (
          <div
            key={s.id}
            className="rounded-lg border border-border-default bg-bg-surface p-4 flex flex-col gap-3"
          >
            <div>
              <div className="flex items-center gap-2 mb-1">
                <h4 className="text-sm font-medium text-text-primary truncate">{s.name}</h4>
                <span
                  className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
                    s.enabled
                      ? 'bg-status-success/10 text-status-success'
                      : 'bg-text-muted/10 text-text-muted'
                  }`}
                >
                  {s.enabled ? 'Active' : 'Inactive'}
                </span>
              </div>
              {s.description && (
                <p className="text-xs text-text-muted line-clamp-2">{s.description}</p>
              )}
            </div>
            <div className="flex gap-2 mt-auto">
              <Button
                variant="outline"
                size="sm"
                onClick={() => handleEdit(s.id)}
                loading={loadingId === s.id}
              >
                <Edit2 className="h-3 w-3" />
                Edit
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => handleDelete(s.id, s.name)}
                loading={deletingId === s.id}
                className="text-status-error hover:bg-status-error/10"
              >
                <Trash2 className="h-3 w-3" />
                Delete
              </Button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
