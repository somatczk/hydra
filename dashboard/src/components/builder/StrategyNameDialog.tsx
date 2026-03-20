'use client';

import { useState, useEffect } from 'react';
import { X, Save } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { fetchApi } from '@/lib/api';
import { logger } from '@/lib/logger';
import type { BuilderState, SerializedConditionGroup } from './types';

interface StrategyNameDialogProps {
  open: boolean;
  onClose: () => void;
  state: BuilderState;
  onSaved: (name: string) => void;
  editingId?: string | null;
  initialName?: string;
  initialDescription?: string;
  initialExchangeId?: string;
  initialSymbol?: string;
}

const EXCHANGE_OPTIONS = [
  { value: 'binance', label: 'Binance' },
  { value: 'bybit', label: 'Bybit' },
  { value: 'kraken', label: 'Kraken' },
  { value: 'okx', label: 'OKX' },
];

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

export function StrategyNameDialog({
  open,
  onClose,
  state,
  onSaved,
  editingId,
  initialName,
  initialDescription,
  initialExchangeId,
  initialSymbol,
}: StrategyNameDialogProps) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [exchangeId, setExchangeId] = useState('binance');
  const [symbol, setSymbol] = useState('BTCUSDT');
  const [enableImmediately, setEnableImmediately] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setName(initialName || '');
      setDescription(initialDescription || '');
      setExchangeId(initialExchangeId || 'binance');
      setSymbol(initialSymbol || 'BTCUSDT');
      setError(null);
    }
  }, [open, initialName, initialDescription, initialExchangeId, initialSymbol]);

  if (!open) return null;

  const isEditing = !!editingId;

  const handleSave = async () => {
    if (!name.trim()) {
      setError('Strategy name is required');
      return;
    }

    setSaving(true);
    setError(null);

    const requestBody = {
      name: name.trim(),
      description: description.trim(),
      exchange_id: exchangeId,
      symbol: symbol.trim() || 'BTCUSDT',
      rules: {
        entry_long: serializeGroup(state.entryLong),
        exit_long: serializeGroup(state.exitLong),
        entry_short: serializeGroup(state.entryShort),
        exit_short: serializeGroup(state.exitShort),
      },
      timeframes: {
        primary: state.timeframes.primary,
        ...(state.timeframes.confirmation
          ? { confirmation: state.timeframes.confirmation }
          : {}),
        ...(state.timeframes.entry ? { entry: state.timeframes.entry } : {}),
      },
      risk: {
        stop_loss_method: state.risk.stopLoss.method,
        stop_loss_value: state.risk.stopLoss.value,
        take_profit_method: state.risk.takeProfit.method,
        take_profit_value: state.risk.takeProfit.value,
        sizing_method: state.risk.sizing.method,
        sizing_params: {
          risk_per_trade_pct: state.risk.sizing.riskPerTradePct,
          max_position_pct: state.risk.sizing.maxPositionPct,
        },
      },
      enable_immediately: enableImmediately,
    };

    try {
      const url = isEditing
        ? `/api/strategies/${editingId}`
        : '/api/strategies/save';
      const method = isEditing ? 'PUT' : 'POST';

      await fetchApi(url, {
        method,
        body: JSON.stringify(requestBody),
      });

      logger.info('Builder', `Strategy "${name}" ${isEditing ? 'updated' : 'saved'}`);
      onSaved(name);
      onClose();
    } catch (err) {
      logger.error('Builder', 'Failed to save strategy', err);
      setError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/50"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Dialog */}
      <div className="relative w-full max-w-lg rounded-xl border border-border-default bg-bg-elevated p-6 shadow-xl mx-4">
        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-text-primary">
            {isEditing ? 'Edit Strategy' : 'Save Strategy'}
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1 text-text-muted hover:text-text-primary hover:bg-bg-hover transition-colors"
            aria-label="Close dialog"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Form */}
        <div className="flex flex-col gap-4">
          <Input
            label="Strategy Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="My RSI Strategy"
            autoFocus
          />
          <Input
            label="Description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Brief description of the strategy..."
          />
          <div className="grid grid-cols-2 gap-4">
            <Select
              label="Exchange"
              options={EXCHANGE_OPTIONS}
              value={exchangeId}
              onChange={(e) => setExchangeId(e.target.value)}
            />
            <Input
              label="Symbol"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              placeholder="BTCUSDT"
            />
          </div>

          {/* Enable toggle */}
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={enableImmediately}
              onChange={(e) => setEnableImmediately(e.target.checked)}
              className="h-4 w-4 rounded border-border-default text-accent-primary focus:ring-accent-primary"
            />
            <span className="text-sm text-text-secondary">Enable strategy immediately</span>
          </label>

          {/* Error */}
          {error && (
            <div className="rounded-lg border border-status-error/30 bg-status-error/5 p-3">
              <p className="text-sm text-status-error">{error}</p>
            </div>
          )}

          {/* Actions */}
          <div className="flex justify-end gap-3 mt-2">
            <Button variant="outline" onClick={onClose} disabled={saving}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleSave} loading={saving}>
              <Save className="h-4 w-4" />
              {isEditing ? 'Update Strategy' : 'Save Strategy'}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
