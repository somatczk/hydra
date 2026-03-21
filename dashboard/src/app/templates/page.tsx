'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Layout, Download } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { fetchApi } from '@/lib/api';
import { useToast } from '@/components/ui/Toast';
import { logger } from '@/lib/logger';

/* ---------- Types ---------- */

interface Template {
  id: string;
  name: string;
  description: string;
  type: string;
  tags: string[];
  author: string;
}

/* ---------- Helpers ---------- */

function typeBadgeVariant(type: string): 'success' | 'info' | 'warning' | 'neutral' {
  switch (type.toLowerCase()) {
    case 'rule-based':
      return 'success';
    case 'dca':
      return 'info';
    case 'grid':
      return 'warning';
    default:
      return 'neutral';
  }
}

/* ---------- Page ---------- */

export default function TemplatesPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [templates, setTemplates] = useState<Template[]>([]);
  const [loading, setLoading] = useState(true);
  const [importingId, setImportingId] = useState<string | null>(null);

  useEffect(() => {
    fetchApi<Template[]>('/api/templates')
      .then(setTemplates)
      .catch((err) => {
        logger.warn('Templates', 'Failed to fetch templates', err);
      })
      .finally(() => setLoading(false));
  }, []);

  const handleImport = async (templateId: string) => {
    setImportingId(templateId);
    try {
      const result = await fetchApi<{ strategy_id: string }>(`/api/templates/${templateId}/import`, {
        method: 'POST',
      });
      toast('success', 'Template imported successfully');
      router.push(`/builder?strategy=${result.strategy_id}`);
    } catch (err) {
      logger.error('Templates', 'Failed to import template', err);
      toast('error', 'Failed to import template');
    } finally {
      setImportingId(null);
    }
  };

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-text-muted">Loading templates...</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Header */}
      <div>
        <p className="text-sm text-text-muted">
          {templates.length} template{templates.length !== 1 ? 's' : ''} available
        </p>
      </div>

      {/* Template cards grid */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {templates.map((template) => (
          <div
            key={template.id}
            className="rounded-xl border border-border-default bg-bg-elevated p-5 transition-colors hover:border-border-hover"
          >
            {/* Card header */}
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-bg-tertiary">
                  <Layout className="h-5 w-5 text-accent-primary" aria-hidden="true" />
                </div>
                <div>
                  <h3 className="text-sm font-semibold text-text-primary">
                    {template.name}
                  </h3>
                  <p className="mt-0.5 text-xs text-text-muted">
                    by {template.author}
                  </p>
                </div>
              </div>
              <StatusBadge
                status={template.type}
                variant={typeBadgeVariant(template.type)}
                size="sm"
              />
            </div>

            {/* Description */}
            <p className="mt-3 text-sm text-text-secondary line-clamp-2">
              {template.description}
            </p>

            {/* Tags */}
            {template.tags.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {template.tags.map((tag) => (
                  <span
                    key={tag}
                    className="rounded-full bg-bg-tertiary px-2.5 py-0.5 text-xs text-text-muted"
                  >
                    {tag}
                  </span>
                ))}
              </div>
            )}

            {/* Import button */}
            <div className="mt-4 border-t border-border-default pt-3">
              <Button
                variant="outline"
                size="sm"
                onClick={() => handleImport(template.id)}
                loading={importingId === template.id}
              >
                <Download className="h-3.5 w-3.5" />
                Import
              </Button>
            </div>
          </div>
        ))}
      </div>

      {templates.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16">
          <Layout className="h-10 w-10 text-text-light" />
          <p className="mt-3 text-sm text-text-muted">No templates available yet</p>
        </div>
      )}
    </div>
  );
}
