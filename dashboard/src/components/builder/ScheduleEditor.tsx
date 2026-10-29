'use client';

import { DataCard } from '@/components/ui/DataCard';
import { cn } from '@/components/ui/cn';
import { ALL_DAYS, ALL_HOURS, DAY_NAMES, scheduleProblem, type Schedule } from '@/lib/schedule';

interface ScheduleEditorProps {
  schedule: Schedule | null;
  primary: string;
  onChange: (schedule: Schedule | null) => void;
}

function toggle(values: number[], v: number): number[] {
  return values.includes(v) ? values.filter((x) => x !== v) : [...values, v];
}

function Chips({ label, values, selected, name, onToggle, className }: {
  label: string;
  values: number[];
  selected: number[];
  name: (v: number) => string;
  onToggle: (v: number) => void;
  className: string;
}) {
  return (
    <div role="group" aria-label={label} className={cn('grid gap-1.5', className)}>
      {values.map((v) => (
        <button
          key={v}
          type="button"
          aria-pressed={selected.includes(v)}
          onClick={() => onToggle(v)}
          className={cn(
            'h-8 rounded border text-sm font-medium tabular-nums max-lg:h-11',
            selected.includes(v) ? 'border-accent bg-hover text-ink-1' : 'border-line-strong text-ink-2 hover:bg-hover',
          )}
        >
          {name(v)}
        </button>
      ))}
    </div>
  );
}

/** The strategy `schedule` block: which primary bars (by their UTC open time) may open a position. */
export function ScheduleEditor({ schedule, primary, onChange }: ScheduleEditorProps) {
  const problem = scheduleProblem(schedule, primary);
  return (
    <DataCard
      title="Trading schedule"
      description="Limit when entries may open. Exits, protective stops, flatten and the kill switch always run."
    >
      <label className="flex cursor-pointer items-center gap-2 max-lg:min-h-11">
        <input
          type="checkbox"
          checked={schedule !== null}
          onChange={(e) => onChange(e.target.checked ? { hours: ALL_HOURS, days: ALL_DAYS } : null)}
          className="h-4 w-4 rounded border-line text-accent focus:ring-accent"
        />
        <span className="text-sm text-ink-2">Only open entries at these times</span>
      </label>

      {schedule !== null && (
        <div className="mt-4 flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <p className="text-sm font-medium text-ink-2">Hours (UTC, the bar&apos;s open time)</p>
            <Chips
              label="Entry hours (UTC)"
              values={ALL_HOURS}
              selected={schedule.hours}
              name={(h) => String(h).padStart(2, '0')}
              onToggle={(h) => onChange({ ...schedule, hours: toggle(schedule.hours, h) })}
              className="grid-cols-6 md:grid-cols-12"
            />
            <p className="text-xs text-ink-2">
              The entry is decided and fills at that bar&apos;s close: hour 13 on 1h bars fills at 14:00. 4h bars open at
              00/04/08/12/16/20 and daily bars at 00, so there only the days select.
            </p>
          </div>
          <div className="flex flex-col gap-1.5">
            <p className="text-sm font-medium text-ink-2">Days</p>
            <Chips
              label="Entry days"
              values={ALL_DAYS}
              selected={schedule.days}
              name={(d) => DAY_NAMES[d]}
              onToggle={(d) => onChange({ ...schedule, days: toggle(schedule.days, d) })}
              className="grid-cols-7"
            />
          </div>
          {problem && (
            <p role="alert" className="text-sm text-danger">
              {problem}
            </p>
          )}
          <p className="text-xs text-ink-2">The signal preview does not apply the schedule.</p>
        </div>
      )}
    </DataCard>
  );
}
