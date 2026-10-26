'use client';

import { useState, useEffect } from 'react';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { fetchApi } from '@/lib/api';
import { describeApiError } from '@/lib/apiState';
import { logger } from '@/lib/logger';
import type { BuilderState, SerializedConditionGroup } from './types';

interface StrategyNameDialogProps {
  open: boolean;
  onClose: () => void;
  state: BuilderState;
  onSaved: (name: string, id: string) => void;
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
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setName(initialName || '');
      setDescription(initialDescription || '');
      setExchangeId(initialExchangeId || 'binance');
      setSymbol(initialSymbol || 'BTCUSDT');
      setEnableImmediately(false);
      setError(null);
    }
  }, [open, initialName, initialDescription, initialExchangeId, initialSymbol]);

  const isEditing = !!editingId;

  const handleSave = async () => {
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
      ...(state.mlOverlay
        ? {
            ml_overlay: {
              model_name: state.mlOverlay.modelName,
              confidence_threshold: state.mlOverlay.confidenceThreshold,
            },
          }
        : {}),
    };

    try {
      const url = isEditing
        ? `/api/strategies/${editingId}`
        : '/api/strategies/save';
      const method = isEditing ? 'PUT' : 'POST';

      const response = await fetchApi<{ id: string }>(url, {
        method,
        body: JSON.stringify(requestBody),
      });

      const savedId = isEditing ? editingId! : response.id;
      logger.info('Builder', `Strategy "${name}" ${isEditing ? 'updated' : 'saved'}`);
      onSaved(name, savedId);
      onClose();
    } catch (err) {
      logger.error('Builder', 'Failed to save strategy', err);
      setError(describeApiError(err, 'Save failed'));
    }
  };

  return (
    <ConfirmDialog
      open={open}
      title={isEditing ? 'Update strategy' : 'Save strategy'}
      primaryLabel="Save strategy"
      secondaryLabel="Cancel"
      pendingLabel="Saving…"
      pendingHint="Closing this won't cancel the save."
      primaryDisabled={!name.trim()}
      onConfirm={handleSave}
      onClose={onClose}
    >
      <Input
        label="Strategy Name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="My RSI Strategy"
      />
      <Input
        label="Description"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        placeholder="Brief description of the strategy..."
      />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
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
      <label className="flex items-center gap-2 cursor-pointer max-lg:min-h-11">
        <input
          type="checkbox"
          checked={enableImmediately}
          onChange={(e) => setEnableImmediately(e.target.checked)}
          className="h-4 w-4 rounded border-line text-accent focus:ring-accent"
        />
        <span className="text-sm text-ink-2">Enable strategy immediately</span>
      </label>

      {/* Error */}
      {error && (
        <p role="alert" className="rounded-lg border border-danger bg-danger-soft p-3 text-sm text-danger">
          {error}
        </p>
      )}
    </ConfirmDialog>
  );
}
