'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { LayoutTemplate, Download } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { ResearchNav } from '@/components/research/ResearchNav';
import { usePoll } from '@/hooks/usePoll';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { describeApiError } from '@/lib/apiState';
import { logger } from '@/lib/logger';
import { money } from '@/lib/format';

/* ---------- Types ---------- */

/** routes/templates.py TemplateMeta — the fields the API really returns. */
interface TemplateMeta {
  id: string;
  name: string;
  description: string;
  type: string;
  risk_level: string;
  expected_sharpe: number | null;
  expected_max_drawdown: number | null;
  recommended_capital: number | null;
  symbols: string[];
  timeframe: string;
}

/* ---------- Helpers ---------- */

function typeLabel(type: string): string {
  return type.toLowerCase() === 'rule_based' ? 'Rule-based' : type;
}

function typeBadgeVariant(type: string): 'success' | 'neutral' {
  return type.toLowerCase() === 'rule_based' ? 'success' : 'neutral';
}

function riskBadgeVariant(riskLevel: string): 'info' | 'warning' | 'error' | 'neutral' {
  switch (riskLevel.toLowerCase()) {
    case 'conservative':
      return 'info';
    case 'moderate':
      return 'warning';
    case 'aggressive':
      return 'error';
    default:
      return 'neutral';
  }
}

/* ---------- Page ---------- */

export default function TemplatesPage() {
  const router = useRouter();
  const { toast } = useToast();
  const templates = usePoll<TemplateMeta[]>('/api/templates', 60_000);
  const [importingId, setImportingId] = useState<string | null>(null);

  const handleImport = async (templateId: string) => {
    setImportingId(templateId);
    try {
      const result = await fetchApi<{ id: string }>(`/api/templates/${templateId}/import`, {
        method: 'POST',
      });
      toast('success', 'Template imported successfully');
      router.push(`/builder?strategy=${result.id}`);
    } catch (err) {
      logger.error('Templates', 'Failed to import template', err);
      toast('error', `Import failed: ${describeApiError(err, 'unknown error')}`);
    } finally {
      setImportingId(null);
    }
  };

  const list = templates.data;
  const loading = templates.state === 'loading';

  return (
    <div className="flex flex-col gap-6">
      <ResearchNav current="/templates" />
      <div>
        <h1 className="text-xl font-semibold text-ink-1">Templates</h1>
        <p className="mt-1 text-sm text-ink-2">
          {loading
            ? 'Loading...'
            : templates.error !== null
              ? 'Templates unavailable'
              : `${(list ?? []).length} template${(list ?? []).length !== 1 ? 's' : ''} available`}
        </p>
      </div>

      {templates.error !== null && (
        <DataStateErrorCard
          error={templates.error}
          fallback="Failed to load templates"
          onRetry={() => void templates.refetch()}
        />
      )}

      {templates.error === null && !loading && (list ?? []).length === 0 && (
        <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-line bg-surface py-16 text-center">
          <LayoutTemplate className="h-10 w-10 text-ink-3" aria-hidden="true" />
          <p className="text-sm text-ink-2">No templates available yet</p>
        </div>
      )}

      {/* Template cards grid */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {(list ?? []).map((template) => (
          <div
            key={template.id}
            className="flex flex-col rounded-lg border border-line bg-surface p-5 transition-colors hover:border-line-strong"
          >
            {/* Card header */}
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 items-center gap-3">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-raised">
                  <LayoutTemplate className="h-5 w-5 text-accent" aria-hidden="true" />
                </div>
                <div className="min-w-0">
                  <h2 className="text-sm font-semibold text-ink-1">
                    {template.name}
                  </h2>
                  <p className="mt-0.5 text-xs text-ink-2">
                    {template.symbols.join(', ')} · {template.timeframe}
                  </p>
                </div>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1">
                <StatusBadge
                  status={typeLabel(template.type)}
                  variant={typeBadgeVariant(template.type)}
                  size="sm"
                />
                <StatusBadge
                  status={template.risk_level}
                  variant={riskBadgeVariant(template.risk_level)}
                  size="sm"
                />
              </div>
            </div>

            {/* Description */}
            <p className="mt-3 text-sm text-ink-2 line-clamp-2">
              {template.description}
            </p>

            {/* Template expectations */}
            <div className="mt-3 grid grid-cols-3 gap-2 rounded-lg border border-line bg-surface p-2.5">
              <div className="text-center">
                <p className="flex items-center justify-center gap-1 text-sm font-bold text-ink-1">
                  {template.expected_sharpe === null ? '—' : template.expected_sharpe.toFixed(2)}
                  {template.expected_sharpe === null && (
                    <DataStateBadge state="no_data" reason="This template sets no expected Sharpe" />
                  )}
                </p>
                <p className="text-xs text-ink-2">Expected Sharpe</p>
              </div>
              <div className="text-center">
                <p className="flex items-center justify-center gap-1 text-sm font-bold text-ink-1">
                  {template.expected_max_drawdown === null ? '—' : `${template.expected_max_drawdown.toFixed(1)}%`}
                  {template.expected_max_drawdown === null && (
                    <DataStateBadge state="no_data" reason="This template sets no expected max drawdown" />
                  )}
                </p>
                <p className="text-xs text-ink-2">Expected max DD</p>
              </div>
              <div className="text-center">
                <p className="flex items-center justify-center gap-1 text-sm font-bold text-ink-1">
                  {template.recommended_capital === null ? '—' : money(template.recommended_capital)}
                  {template.recommended_capital === null && (
                    <DataStateBadge state="no_data" reason="This template sets no recommended capital" />
                  )}
                </p>
                <p className="text-xs text-ink-2">Capital (USDT)</p>
              </div>
            </div>

            {/* Import button */}
            <div className="mt-4 border-t border-line pt-3">
              <Button
                variant="outline"
                size="sm"
                onClick={() => handleImport(template.id)}
                loading={importingId === template.id}
              >
                <Download className="h-3.5 w-3.5" aria-hidden="true" />
                Import
              </Button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
