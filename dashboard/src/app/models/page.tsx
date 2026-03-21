'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { Brain, ArrowUpCircle, RotateCcw, RefreshCw, Upload } from 'lucide-react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Cell,
} from 'recharts';
import { DataCard } from '@/components/ui/DataCard';
import { Table } from '@/components/ui/Table';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { Button } from '@/components/ui/Button';
import { ErrorCard } from '@/components/ui/ErrorCard';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { logger } from '@/lib/logger';

/* ---------- Types ---------- */

interface Model {
  id: string;
  name: string;
  version: string;
  status: string;
  statusVariant: 'success' | 'warning' | 'info' | 'neutral';
  accuracy: string;
  precision: string;
  drift: string;
  driftStatus: 'success' | 'warning' | 'error';
  lastTrained: string;
  fileSize: string;
}

interface ApiModel {
  id: string;
  name: string;
  version: string;
  stage: string;
  metrics: { accuracy: number | null; precision: number | null };
  drift: number | null;
  drift_status: string;
  last_trained: string;
  file_size: number;
  file_name: string;
}

/* ---------- Helpers ---------- */

function stageToVariant(stage: string): 'success' | 'warning' | 'info' | 'neutral' {
  switch (stage) {
    case 'Production': return 'success';
    case 'Staging': return 'info';
    case 'Training': return 'warning';
    default: return 'neutral';
  }
}

function driftToStatus(ds: string): 'success' | 'warning' | 'error' {
  switch (ds) {
    case 'moderate': return 'warning';
    case 'high': return 'error';
    default: return 'success';
  }
}

function fmtFileSize(bytes: number): string {
  if (bytes === 0) return '\u2014';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function mapApiModels(data: ApiModel[]): Model[] {
  return data.map((m) => ({
    id: m.id,
    name: m.name,
    version: m.version,
    status: m.stage,
    statusVariant: stageToVariant(m.stage),
    accuracy: m.metrics.accuracy != null ? `${m.metrics.accuracy}%` : 'N/A',
    precision: m.metrics.precision != null ? `${m.metrics.precision}%` : 'N/A',
    drift: m.drift != null ? m.drift.toFixed(2) : 'N/A',
    driftStatus: driftToStatus(m.drift_status),
    lastTrained: m.last_trained,
    fileSize: fmtFileSize(m.file_size ?? 0),
  }));
}

const driftLabels: Record<string, string> = {
  success: 'Low',
  warning: 'Moderate',
  error: 'High',
};

/* ---------- Page ---------- */

export default function ModelsPage() {
  const { toast } = useToast();
  useEffect(() => { logger.info('Models', 'Page mounted'); }, []);
  const [models, setModels] = useState<Model[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [isDark, setIsDark] = useState(false);
  const [fetchErrors, setFetchErrors] = useState<Record<string, boolean>>({});
  const [actionLoading, setActionLoading] = useState<Record<string, string | null>>({});
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains('dark'));
  }, []);

  const loadData = useCallback(async () => {
    setLoading(true);
    setFetchErrors({});

    await fetchApi<ApiModel[]>('/api/models')
      .then((data) => setModels(mapApiModels(data)))
      .catch(() => { setFetchErrors((prev) => ({ ...prev, models: true })); })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const handlePromote = async (model: Model) => {
    if (!confirm(`Promote "${model.name}" ${model.version} to production?`)) return;
    setActionLoading((prev) => ({ ...prev, [model.id]: 'promote' }));
    try {
      await fetchApi(`/api/models/${model.id}/promote`, { method: 'POST' });
      toast('success', `${model.name} promoted to production`);
      loadData();
    } catch (err) {
      logger.error('Models', `Failed to promote ${model.name}`, err);
      toast('error', `Failed to promote ${model.name}`);
    } finally {
      setActionLoading((prev) => ({ ...prev, [model.id]: null }));
    }
  };

  const handleRollback = async (model: Model) => {
    if (!confirm(`ROLLBACK "${model.name}" ${model.version}? This will revert to the previous version.`)) return;
    setActionLoading((prev) => ({ ...prev, [model.id]: 'rollback' }));
    try {
      await fetchApi(`/api/models/${model.id}/rollback`, { method: 'POST' });
      toast('success', `${model.name} rolled back`);
      loadData();
    } catch (err) {
      logger.error('Models', `Failed to rollback ${model.name}`, err);
      toast('error', `Failed to rollback ${model.name}`);
    } finally {
      setActionLoading((prev) => ({ ...prev, [model.id]: null }));
    }
  };

  const handleRetrain = async (model: Model) => {
    if (!confirm(`Start retraining for "${model.name}"?`)) return;
    setActionLoading((prev) => ({ ...prev, [model.id]: 'retrain' }));
    try {
      await fetchApi(`/api/models/${model.id}/retrain`, { method: 'POST' });
      toast('success', `Retraining started for ${model.name}`);
      loadData();
    } catch (err) {
      logger.error('Models', `Failed to retrain ${model.name}`, err);
      toast('error', `Failed to retrain ${model.name}`);
    } finally {
      setActionLoading((prev) => ({ ...prev, [model.id]: null }));
    }
  };

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const formData = new FormData();
      formData.append('file', file);
      await fetch(`${process.env.NEXT_PUBLIC_API_URL || ''}/api/models/upload`, {
        method: 'POST',
        body: formData,
        credentials: 'include',
      }).then((res) => {
        if (!res.ok) return res.json().then((d) => { throw new Error(d.detail || 'Upload failed'); });
        return res.json();
      });
      toast('success', `Model "${file.name}" uploaded`);
      loadData();
    } catch (err) {
      logger.error('Models', 'Failed to upload model', err);
      toast('error', `Upload failed: ${err instanceof Error ? err.message : 'unknown error'}`);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const columns = [
    {
      key: 'name',
      header: 'Model',
      render: (row: Model) => (
        <div className="flex items-center gap-2">
          <Brain className="h-4 w-4 text-text-muted shrink-0" aria-hidden="true" />
          <div>
            <p className="text-sm font-medium text-text-primary">{row.name}</p>
            <p className="text-xs text-text-muted">{row.version}</p>
          </div>
        </div>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      render: (row: Model) => (
        <StatusBadge status={row.status} variant={row.statusVariant} size="sm" />
      ),
    },
    { key: 'accuracy', header: 'Accuracy' },
    { key: 'precision', header: 'Precision', hideOnMobile: true },
    { key: 'fileSize', header: 'Size', hideOnMobile: true },
    {
      key: 'drift',
      header: 'Drift',
      render: (row: Model) => (
        <div className="flex items-center gap-2">
          <span className="text-sm text-text-primary">{row.drift}</span>
          <StatusBadge
            status={driftLabels[row.driftStatus]}
            variant={row.driftStatus}
            size="sm"
          />
        </div>
      ),
    },
    { key: 'lastTrained', header: 'Last Trained', hideOnMobile: true },
    {
      key: 'actions',
      header: 'Actions',
      render: (row: Model) => {
        const currentAction = actionLoading[row.id];
        return (
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => handlePromote(row)}
              loading={currentAction === 'promote'}
              disabled={!!currentAction || row.status === 'Production'}
            >
              <ArrowUpCircle className="h-3.5 w-3.5" />
              <span className="hidden lg:inline">Promote</span>
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => handleRollback(row)}
              loading={currentAction === 'rollback'}
              disabled={!!currentAction}
            >
              <RotateCcw className="h-3.5 w-3.5" />
              <span className="hidden lg:inline">Rollback</span>
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => handleRetrain(row)}
              loading={currentAction === 'retrain'}
              disabled={!!currentAction || row.status === 'Training'}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              <span className="hidden lg:inline">Retrain</span>
            </Button>
          </div>
        );
      },
    },
  ];

  const accuracyChartData = models
    ? models.map((m) => ({ name: m.name, accuracy: parseFloat(m.accuracy) || 0 })).filter((m) => m.accuracy > 0)
    : [];

  const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(226,232,240,0.8)';
  const textMuted = isDark ? 'rgba(255,255,255,0.44)' : '#475569';

  return (
    <div className="flex flex-col gap-6">
      {/* Header with upload */}
      <div className="flex items-center justify-between">
        <div />
        <div>
          <input
            ref={fileInputRef}
            type="file"
            accept=".onnx"
            className="hidden"
            onChange={handleUpload}
          />
          <Button
            variant="primary"
            onClick={() => fileInputRef.current?.click()}
            loading={uploading}
          >
            <Upload className="h-4 w-4" />
            Upload Model
          </Button>
        </div>
      </div>

      {/* Model accuracy chart */}
      <DataCard title="Model Performance" description="Accuracy over time across deployed models">
        {fetchErrors.models ? (
          <ErrorCard message="Failed to load model data" onRetry={loadData} />
        ) : loading ? (
          <div className="h-56 animate-pulse rounded-lg bg-bg-tertiary" />
        ) : accuracyChartData.length > 0 ? (
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={accuracyChartData}
                margin={{ top: 4, right: 4, left: 0, bottom: 0 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} vertical={false} />
                <XAxis
                  dataKey="name"
                  tickFormatter={(name: string) => name.length > 14 ? `${name.slice(0, 13)}...` : name}
                  tick={{ fontSize: 11, fill: textMuted }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  domain={[0, 100]}
                  tickFormatter={(v: number) => `${v}%`}
                  tick={{ fontSize: 11, fill: textMuted }}
                  axisLine={false}
                  tickLine={false}
                  width={36}
                />
                <Tooltip
                  contentStyle={{
                    background: isDark ? '#1e2433' : '#ffffff',
                    border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : '#e2e8f0'}`,
                    borderRadius: '8px',
                    fontSize: '12px',
                  }}
                  formatter={(v: number) => [`${v.toFixed(1)}%`, 'Accuracy']}
                />
                <Bar dataKey="accuracy" radius={[3, 3, 0, 0]}>
                  {accuracyChartData.map((entry, index) => (
                    <Cell
                      key={`cell-${index}`}
                      fill={entry.accuracy > 70 ? '#22c55e' : entry.accuracy >= 60 ? '#f59e0b' : '#ef4444'}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>

      {/* Model registry table */}
      <DataCard title="Model Registry" description="All registered ML models">
        {fetchErrors.models ? (
          <ErrorCard message="Failed to load models" onRetry={loadData} />
        ) : loading ? (
          <div className="space-y-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-12 animate-pulse rounded-lg bg-bg-tertiary" />
            ))}
          </div>
        ) : models && models.length > 0 ? (
          <Table
            columns={columns}
            data={models}
            keyExtractor={(row) => row.id}
          />
        ) : (
          <p className="text-sm text-text-muted text-center py-8">No data yet</p>
        )}
      </DataCard>
    </div>
  );
}
