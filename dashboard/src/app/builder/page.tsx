import { Wrench, Eye, Save, Plus } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { DataCard } from '@/components/ui/DataCard';
import { Select } from '@/components/ui/Select';
import { Input } from '@/components/ui/Input';

/* ---------- Page ---------- */

export default function BuilderPage() {
  return (
    <div className="flex flex-col gap-6">
      {/* Indicator Picker */}
      <DataCard title="Indicator Configuration" description="Select and configure technical indicators">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          <Select
            label="Primary Indicator"
            options={[
              { value: 'rsi', label: 'RSI (Relative Strength Index)' },
              { value: 'macd', label: 'MACD' },
              { value: 'bb', label: 'Bollinger Bands' },
              { value: 'ema', label: 'EMA (Exponential Moving Average)' },
              { value: 'atr', label: 'ATR (Average True Range)' },
            ]}
            placeholder="Select indicator..."
            defaultValue="rsi"
          />
          <Input label="Period" type="number" placeholder="14" defaultValue="14" />
          <Input label="Threshold" type="number" placeholder="70" defaultValue="70" />
        </div>
        <div className="mt-4">
          <Button variant="ghost" size="sm">
            <Plus className="h-4 w-4" />
            Add Indicator
          </Button>
        </div>
      </DataCard>

      {/* Condition Builder */}
      <DataCard title="Entry/Exit Conditions" description="Define when to enter and exit trades">
        <div className="space-y-4">
          <div className="rounded-lg border border-border-default bg-bg-secondary p-4">
            <h4 className="text-sm font-semibold text-text-primary mb-3">Entry Conditions</h4>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              <Select
                label="Indicator"
                options={[
                  { value: 'rsi', label: 'RSI' },
                  { value: 'macd', label: 'MACD Signal' },
                  { value: 'price', label: 'Price' },
                ]}
                defaultValue="rsi"
              />
              <Select
                label="Condition"
                options={[
                  { value: 'crosses_below', label: 'Crosses Below' },
                  { value: 'crosses_above', label: 'Crosses Above' },
                  { value: 'greater_than', label: 'Greater Than' },
                  { value: 'less_than', label: 'Less Than' },
                ]}
                defaultValue="crosses_below"
              />
              <Input label="Value" type="number" placeholder="30" defaultValue="30" />
            </div>
          </div>

          <div className="rounded-lg border border-border-default bg-bg-secondary p-4">
            <h4 className="text-sm font-semibold text-text-primary mb-3">Exit Conditions</h4>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              <Select
                label="Indicator"
                options={[
                  { value: 'rsi', label: 'RSI' },
                  { value: 'macd', label: 'MACD Signal' },
                  { value: 'price', label: 'Price' },
                ]}
                defaultValue="rsi"
              />
              <Select
                label="Condition"
                options={[
                  { value: 'crosses_above', label: 'Crosses Above' },
                  { value: 'crosses_below', label: 'Crosses Below' },
                  { value: 'greater_than', label: 'Greater Than' },
                  { value: 'less_than', label: 'Less Than' },
                ]}
                defaultValue="crosses_above"
              />
              <Input label="Value" type="number" placeholder="70" defaultValue="70" />
            </div>
          </div>
        </div>
      </DataCard>

      {/* Timeframe Selector */}
      <DataCard title="Timeframe" description="Select the candlestick timeframe for analysis">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Select
            label="Primary Timeframe"
            options={[
              { value: '1m', label: '1 Minute' },
              { value: '5m', label: '5 Minutes' },
              { value: '15m', label: '15 Minutes' },
              { value: '1h', label: '1 Hour' },
              { value: '4h', label: '4 Hours' },
              { value: '1d', label: '1 Day' },
            ]}
            defaultValue="1h"
          />
          <Select
            label="Confirmation Timeframe"
            options={[
              { value: 'none', label: 'None' },
              { value: '15m', label: '15 Minutes' },
              { value: '1h', label: '1 Hour' },
              { value: '4h', label: '4 Hours' },
              { value: '1d', label: '1 Day' },
            ]}
            defaultValue="4h"
          />
        </div>
      </DataCard>

      {/* Risk Configuration */}
      <DataCard title="Risk Configuration" description="Define position sizing and risk parameters">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          <Input label="Max Position Size (%)" type="number" placeholder="5" defaultValue="5" hint="Percentage of portfolio per trade" />
          <Input label="Stop Loss (%)" type="number" placeholder="2" defaultValue="2" hint="Maximum loss before auto-exit" />
          <Input label="Take Profit (%)" type="number" placeholder="6" defaultValue="6" hint="Target profit to auto-close" />
          <Input label="Max Daily Trades" type="number" placeholder="10" defaultValue="10" />
          <Input label="Max Open Positions" type="number" placeholder="3" defaultValue="3" />
          <Input label="Cooldown (minutes)" type="number" placeholder="15" defaultValue="15" hint="Wait time between trades" />
        </div>
      </DataCard>

      {/* Actions */}
      <div className="flex flex-col gap-3 sm:flex-row sm:justify-end">
        <Button variant="outline" size="lg">
          <Eye className="h-4 w-4" />
          Preview Signals
        </Button>
        <Button variant="primary" size="lg">
          <Save className="h-4 w-4" />
          Save Strategy
        </Button>
      </div>
    </div>
  );
}
