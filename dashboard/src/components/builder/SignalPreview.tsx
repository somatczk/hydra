'use client';

import { useState } from 'react';
import { Eye, TrendingUp, TrendingDown, BarChart3 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { DataCard } from '@/components/ui/DataCard';
import { fetchApi } from '@/lib/api';
import { describeApiError } from '@/lib/apiState';
import { formatUtc, type DataProvenance } from '@/components/backtest/DataProvenance';
import type { PreviewResponse, PreviewSignal, BuilderState, SerializedConditionGroup } from './types';

type PreviewResult = PreviewResponse & { provenance?: DataProvenance };

interface SignalPreviewProps {
  state: BuilderState;
}

function serializeGroup(
  group: BuilderState['entryLong'],
): SerializedConditionGroup | null {
  if (group.conditions.length === 0) return null;
  return {
    operator: group.operator,
    conditions: group.conditions.map((c) => ({
      indicator: c.indicator,
      params: c.params,
      comparator: c.comparator,
      value: c.value,
    })),
  };
}

export function SignalPreview({ state }: SignalPreviewProps) {
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handlePreview = async () => {
    setLoading(true);
    setError(null);

    const requestBody = {
      rules: {
        entry_long: serializeGroup(state.entryLong),
        exit_long: serializeGroup(state.exitLong),
        entry_short: serializeGroup(state.entryShort),
        exit_short: serializeGroup(state.exitShort),
      },
      timeframe: state.timeframes.primary,
      exchange_id: state.exchangeId,
      symbol: state.symbol,
      bars_count: 200,
    };

    try {
      setPreview(null);
      const data = await fetchApi<PreviewResult>('/api/strategies/preview', {
        method: 'POST',
        body: JSON.stringify(requestBody),
      });
      setPreview(data);
    } catch (err) {
      setError(describeApiError(err, 'Preview failed'));
    } finally {
      setLoading(false);
    }
  };

  const hasConditions =
    state.entryLong.conditions.length > 0 ||
    state.exitLong.conditions.length > 0 ||
    state.entryShort.conditions.length > 0 ||
    state.exitShort.conditions.length > 0;

  return (
    <DataCard title="Signal Preview" description="Test your strategy on the latest stored bars">
      <div className="flex flex-col gap-4">
        {/* Preview button */}
        <div className="flex flex-wrap items-center gap-3">
          <Button
            variant="outline"
            size="lg"
            onClick={handlePreview}
            loading={loading}
            disabled={!hasConditions}
          >
            <Eye className="h-4 w-4" />
            Preview Signals
          </Button>
          {!hasConditions && (
            <span className="text-xs text-ink-2">
              Add at least one condition to preview
            </span>
          )}
        </div>

        {/* Error message */}
        {error && (
          <div className="rounded-lg border border-danger bg-danger-soft p-3">
            <p className="text-sm text-danger">{error}</p>
          </div>
        )}

        {/* Results */}
        {preview && (
          <div className="flex flex-col gap-4">
            {preview.provenance && (
              <p className="text-xs text-ink-2" data-testid="preview-provenance">
                {preview.provenance.bar_count} stored bars, {preview.provenance.exchange}{' '}
                {preview.provenance.symbol} {preview.provenance.timeframe},{' '}
                {formatUtc(preview.provenance.first_ts)} to {formatUtc(preview.provenance.last_ts)}
                {preview.provenance.gap_count ? ` (${preview.provenance.gap_count} gaps)` : ''}
              </p>
            )}
            {/* Metrics */}
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-lg border border-line bg-surface p-3 text-center">
                <BarChart3 className="mx-auto h-5 w-5 text-ink-2 mb-1" />
                <p className="text-lg font-bold text-ink-1 font-display">
                  {preview.metrics.trades}
                </p>
                <p className="text-xs text-ink-2">Trades</p>
              </div>
              <div className="rounded-lg border border-line bg-surface p-3 text-center">
                <TrendingUp className="mx-auto h-5 w-5 text-ink-2 mb-1" />
                <p className="text-lg font-bold text-ink-1 font-display">
                  {(preview.metrics.win_rate * 100).toFixed(1)}%
                </p>
                <p className="text-xs text-ink-2">Win Rate</p>
              </div>
              <div className="rounded-lg border border-line bg-surface p-3 text-center">
                {preview.metrics.pnl >= 0 ? (
                  <TrendingUp className="mx-auto h-5 w-5 text-ink-1 mb-1" />
                ) : (
                  <TrendingDown className="mx-auto h-5 w-5 text-danger mb-1" />
                )}
                <p
                  className={`text-lg font-bold font-display ${
                    preview.metrics.pnl >= 0 ? 'text-ink-1' : 'text-danger'
                  }`}
                >
                  ${preview.metrics.pnl.toFixed(2)}
                </p>
                <p className="text-xs text-ink-2">PnL</p>
              </div>
            </div>

            {/* Signal chart placeholder */}
            <div className="rounded-lg border border-line bg-surface p-4">
              <h3 className="text-sm font-semibold text-ink-1 mb-3">Signal Timeline</h3>
              {preview.signals.length === 0 ? (
                <p className="text-sm text-ink-2 text-center py-4">
                  No signals generated. Try adjusting your conditions.
                </p>
              ) : (
                <div className="flex flex-col gap-1 max-h-48 overflow-y-auto">
                  {preview.signals.map((signal: PreviewSignal, index: number) => {
                    const isEntry =
                      signal.type === 'entry_long' || signal.type === 'entry_short';
                    const isLong =
                      signal.type === 'entry_long' || signal.type === 'exit_long';
                    return (
                      <div
                        key={`${signal.timestamp}-${index}`}
                        className="flex items-center gap-2 text-xs"
                      >
                        {/* Signal marker */}
                        <span
                          className={`inline-block w-2 h-2 rounded-full ${
                            isEntry
                              ? isLong
                                ? 'bg-ink-1'
                                : 'bg-danger'
                              : 'bg-ink-2'
                          }`}
                        />
                        {/* Arrow indicator */}
                        <span className={isLong ? 'text-ink-1' : 'text-danger'}>
                          {isEntry ? (isLong ? '\u2191' : '\u2193') : '\u2190'}
                        </span>
                        <span className="text-ink-2 w-32 max-lg:w-20 shrink-0 font-mono">
                          {new Date(signal.timestamp).toLocaleDateString()}
                        </span>
                        <span className="text-ink-2 capitalize">
                          {signal.type.replace(/_/g, ' ')}
                        </span>
                        <span className="text-ink-1 font-medium ml-auto font-mono">
                          ${signal.price.toFixed(2)}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </DataCard>
  );
}
