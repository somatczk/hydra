'use client';

import { Select } from '@/components/ui/Select';
import { Input } from '@/components/ui/Input';
import { DataCard } from '@/components/ui/DataCard';
import type { RiskConfig, StopLossConfig, TakeProfitConfig, SizingConfig } from './types';

interface RiskConfiguratorProps {
  risk: RiskConfig;
  onStopLossChange: (stopLoss: Partial<StopLossConfig>) => void;
  onTakeProfitChange: (takeProfit: Partial<TakeProfitConfig>) => void;
  onSizingChange: (sizing: Partial<SizingConfig>) => void;
}

const STOP_LOSS_METHODS = [
  { value: 'atr', label: 'ATR Multiple' },
  { value: 'fixed_pct', label: 'Fixed Percentage' },
];

const TAKE_PROFIT_METHODS = [
  { value: 'atr', label: 'ATR Multiple' },
  { value: 'fixed_pct', label: 'Fixed Percentage' },
];

const SIZING_METHODS = [
  { value: 'fixed_fractional', label: 'Fixed Fractional' },
  { value: 'atr_based', label: 'ATR Based' },
  { value: 'kelly', label: 'Kelly Criterion' },
  { value: 'volatility_scaled', label: 'Volatility Scaled' },
];

export function RiskConfigurator({
  risk,
  onStopLossChange,
  onTakeProfitChange,
  onSizingChange,
}: RiskConfiguratorProps) {
  return (
    <DataCard title="Risk Configuration" description="Define position sizing and risk parameters">
      <div className="flex flex-col gap-6">
        {/* Stop Loss */}
        <div>
          <h4 className="text-sm font-semibold text-text-primary mb-3">Stop Loss</h4>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Select
              label="Method"
              options={STOP_LOSS_METHODS}
              value={risk.stopLoss.method}
              onChange={(e) =>
                onStopLossChange({
                  method: e.target.value as StopLossConfig['method'],
                })
              }
            />
            <Input
              label={risk.stopLoss.method === 'atr' ? 'ATR Multiple' : 'Percentage (%)'}
              type="number"
              value={risk.stopLoss.value}
              min={0.1}
              max={risk.stopLoss.method === 'atr' ? 10 : 50}
              step={0.1}
              onChange={(e) => {
                const val = parseFloat(e.target.value);
                if (!isNaN(val)) onStopLossChange({ value: val });
              }}
              hint={
                risk.stopLoss.method === 'atr'
                  ? 'Multiplier applied to ATR for stop distance'
                  : 'Fixed percentage below entry price'
              }
            />
          </div>
        </div>

        {/* Take Profit */}
        <div>
          <h4 className="text-sm font-semibold text-text-primary mb-3">Take Profit</h4>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Select
              label="Method"
              options={TAKE_PROFIT_METHODS}
              value={risk.takeProfit.method}
              onChange={(e) =>
                onTakeProfitChange({
                  method: e.target.value as TakeProfitConfig['method'],
                })
              }
            />
            <Input
              label={risk.takeProfit.method === 'atr' ? 'ATR Multiple' : 'Percentage (%)'}
              type="number"
              value={risk.takeProfit.value}
              min={0.1}
              max={risk.takeProfit.method === 'atr' ? 20 : 100}
              step={0.1}
              onChange={(e) => {
                const val = parseFloat(e.target.value);
                if (!isNaN(val)) onTakeProfitChange({ value: val });
              }}
              hint={
                risk.takeProfit.method === 'atr'
                  ? 'Multiplier applied to ATR for target distance'
                  : 'Fixed percentage above entry price'
              }
            />
          </div>
        </div>

        {/* Position Sizing */}
        <div>
          <h4 className="text-sm font-semibold text-text-primary mb-3">Position Sizing</h4>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <Select
              label="Method"
              options={SIZING_METHODS}
              value={risk.sizing.method}
              onChange={(e) =>
                onSizingChange({
                  method: e.target.value as SizingConfig['method'],
                })
              }
            />
            <Input
              label="Risk per Trade (%)"
              type="number"
              value={risk.sizing.riskPerTradePct}
              min={0.1}
              max={10}
              step={0.1}
              onChange={(e) => {
                const val = parseFloat(e.target.value);
                if (!isNaN(val)) onSizingChange({ riskPerTradePct: val });
              }}
              hint="Percentage of portfolio risked per trade"
            />
            <Input
              label="Max Position (%)"
              type="number"
              value={risk.sizing.maxPositionPct}
              min={1}
              max={100}
              step={1}
              onChange={(e) => {
                const val = parseFloat(e.target.value);
                if (!isNaN(val)) onSizingChange({ maxPositionPct: val });
              }}
              hint="Maximum position as % of portfolio"
            />
          </div>
        </div>
      </div>
    </DataCard>
  );
}
