'use client';

import { useState, useEffect, useCallback } from 'react';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { fetchApi } from '@/lib/api';
import { describeApiError, parseErrorDetail } from '@/lib/apiState';
import { logger } from '@/lib/logger';
import { UniversePicker, symbolInUniverse, type UniverseSnapshot } from './UniversePicker';
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
  /** P4-F: the stored perp leverage, so an edited binanceusdm strategy round-trips it. */
  initialLeverage?: number;
}

const EXCHANGE_OPTIONS = [
  { value: 'binance', label: 'Binance' },
  { value: 'binanceusdm', label: 'Binance USD⌂-M (perps)' },
  { value: 'bybit', label: 'Bybit' },
  { value: 'kraken', label: 'Kraken' },
  { value: 'okx', label: 'OKX' },
];

const LEVERAGE_OPTIONS = [
  { value: '1', label: '1× (no leverage)' },
  { value: '2', label: '2×' },
  { value: '3', label: '3×' },
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
  initialLeverage,
}: StrategyNameDialogProps) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [exchangeId, setExchangeId] = useState('binance');
  const [symbol, setSymbol] = useState('BTCUSDT');
  const [leverage, setLeverage] = useState('1');
  const [enableImmediately, setEnableImmediately] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [universe, setUniverse] = useState<UniverseSnapshot | null>(null);
  const handleUniverse = useCallback((snapshot: UniverseSnapshot | null) => setUniverse(snapshot), []);

  useEffect(() => {
    if (open) {
      setName(initialName || '');
      setDescription(initialDescription || '');
      setExchangeId(initialExchangeId || 'binance');
      setSymbol(initialSymbol || 'BTCUSDT');
      setLeverage(String(initialLeverage ?? 1));
      setEnableImmediately(false);
      setSaveError(null);
    }
  }, [open, initialName, initialDescription, initialExchangeId, initialSymbol, initialLeverage]);

  const isEditing = !!editingId;
  const perps = exchangeId === 'binanceusdm';
  /* The write gate at the UI layer: only universe members are committable; an unreadable universe leaves the refusal to the engine's 422. */
  const symbolGateBlocks = universe !== null && !symbolInUniverse(symbol.trim(), universe);
  const gate = saveError !== null ? parseErrorDetail(saveError) : null;
  const isUniverseGate = gate?.code === 'symbol_not_in_universe';

  const handleSave = async () => {
    setSaveError(null);

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
      // P4-F: the perps fields — leverage applies to binanceusdm FUTURES only (isolated is the sole
      // mode in 2.8.0); a spot venue always sends 1× or the save refuses it.
      leverage: perps ? Number(leverage) : 1,
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
      setSaveError(err);
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
      primaryDisabled={!name.trim() || symbolGateBlocks}
      onConfirm={handleSave}
      onClose={onClose}
    >
      <Input
        label="Strategy name"
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
        <UniversePicker open={open} value={symbol} onChange={setSymbol} onUniverse={handleUniverse} />
      </div>

      {/* P4-F: the perps fields (venue-gated); the symbol section above is owned by P6-UI. */}
      {perps && (
        <fieldset className="flex flex-col gap-3 rounded-lg border border-line bg-surface p-3">
          <legend className="px-1 text-sm font-medium text-ink-2">Perps (Binance USD⌂-M)</legend>
          <p className="text-xs text-ink-2">
            Market type: FUTURES · Margin mode: isolated (the only mode in 2.8.0). Live perp sessions answer to the
            separate perps arm ceremony, not the spot gate.
          </p>
          <Select
            label="Leverage (1–3×)"
            options={LEVERAGE_OPTIONS}
            value={leverage}
            onChange={(e) => setLeverage(e.target.value)}
          />
          <p className="text-xs text-ink-2">
            The account-wide cap (3×) is enforced again at session start; spot strategies stay 1×.
          </p>
        </fieldset>
      )}

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
      {saveError !== null && isUniverseGate && (
        <div
          role="alert"
          data-testid="universe-gate-error"
          className="rounded-lg border border-danger bg-danger-soft p-3 text-sm text-danger"
        >
          <p>{describeApiError(saveError, 'Save failed')}</p>
          <p>Pick a member of the current top-100 universe from the symbol field above — a symbol outside it can&apos;t be saved.</p>
        </div>
      )}
      {saveError !== null && !isUniverseGate && (
        <p role="alert" className="rounded-lg border border-danger bg-danger-soft p-3 text-sm text-danger">
          {describeApiError(saveError, 'Save failed')}
        </p>
      )}
    </ConfirmDialog>
  );
}
