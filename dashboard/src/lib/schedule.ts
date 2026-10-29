/** The strategy `schedule` block (strategy/config.py ScheduleConfig): an ENTRY filter on the primary bar's open time
 * in UTC — hours 0-23, days 0-6 with Monday=0 (Python `weekday()`, not JS `getUTCDay()`). Exits are never filtered. */

import { timeframeSeconds } from './derive';

/** The wire block as `GET /api/strategies/{id}` returns it; an absent list means every hour or every day. */
export interface WireSchedule {
  active_hours_utc?: number[];
  active_days?: number[];
}

/** The builder's copy: both lists always present; null means no schedule (always active). */
export interface Schedule {
  hours: number[];
  days: number[];
}

export const ALL_HOURS = Array.from({ length: 24 }, (_, h) => h);
export const ALL_DAYS = [0, 1, 2, 3, 4, 5, 6];
export const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

export function scheduleFromWire(wire: WireSchedule | null | undefined): Schedule | null {
  if (!wire) return null;
  return { hours: wire.active_hours_utc ?? ALL_HOURS, days: wire.active_days ?? ALL_DAYS };
}

/** The save/update body's `schedule`: `{}` clears a stored one on update and writes nothing on save. */
export function scheduleToWire(schedule: Schedule | null): WireSchedule {
  if (!schedule) return {};
  const hours = [...schedule.hours].sort((a, b) => a - b);
  const days = [...schedule.days].sort((a, b) => a - b);
  if (hours.length === 24 && days.length === 7) return {};
  return { active_hours_utc: hours, active_days: days };
}

/** Why the engine would refuse this schedule (422 `invalid_strategy`), mirroring StrategyConfig._schedule_can_match. */
export function scheduleProblem(schedule: Schedule | null, primary: string): string | null {
  if (!schedule) return null;
  if (schedule.hours.length === 0) return 'Pick at least one hour.';
  if (schedule.days.length === 0) return 'Pick at least one day.';
  const tf = timeframeSeconds(primary);
  if (tf === null) return null;
  const opens = ALL_HOURS.filter((h) => (h * 3600) % tf === 0);
  if (!schedule.hours.some((h) => opens.includes(h))) {
    return `${primary} bars open only at ${opens.join(', ')} UTC, so no entry could ever pass: include one of those hours.`;
  }
  if (primary === '1w' && !schedule.days.includes(0)) return '1w bars open on Monday: include Monday.';
  return null;
}

/** "13–21, 23" from [13, 14, …, 21, 23]. */
function runs(values: number[], label: (v: number) => string): string {
  const sorted = [...values].sort((a, b) => a - b);
  const parts: string[] = [];
  for (let i = 0; i < sorted.length; i++) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++;
    parts.push(j === i ? label(sorted[i]) : `${label(sorted[i])}–${label(sorted[j])}`);
    i = j;
  }
  return parts.join(', ');
}

/** One line for a stored schedule: "Entries only on bars opening Mon–Fri, 13–21 UTC". */
export function scheduleSummary(wire: WireSchedule | null | undefined): string {
  const s = scheduleFromWire(wire);
  if (!s || (s.hours.length === 24 && s.days.length === 7)) return 'Entries at any time';
  const days = s.days.length === 7 ? 'any day' : runs(s.days, (d) => DAY_NAMES[d] ?? String(d));
  const hours = s.hours.length === 24 ? 'any hour' : `${runs(s.hours, (h) => String(h).padStart(2, '0'))} UTC`;
  return `Entries only on bars opening ${days}, ${hours}`;
}
