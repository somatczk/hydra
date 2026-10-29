import { describe, expect, it } from 'vitest';
import { ALL_DAYS, ALL_HOURS, scheduleFromWire, scheduleProblem, scheduleSummary, scheduleToWire } from './schedule';

const WEEKDAY_SESSION = { hours: [13, 14, 15, 16, 17, 18, 19, 20, 21], days: [0, 1, 2, 3, 4] };

describe('schedule wire mapping', () => {
  it('fills an omitted list with every hour or day, and null stays no schedule', () => {
    expect(scheduleFromWire({ active_hours_utc: [0, 1, 2] })).toEqual({ hours: [0, 1, 2], days: ALL_DAYS });
    expect(scheduleFromWire({ active_days: [5, 6] })).toEqual({ hours: ALL_HOURS, days: [5, 6] });
    expect(scheduleFromWire(null)).toBeNull();
  });

  it('sends sorted lists, and {} for no schedule or an every-hour-every-day one (clears on update)', () => {
    expect(scheduleToWire({ hours: [21, 13], days: [4, 0] })).toEqual({ active_hours_utc: [13, 21], active_days: [0, 4] });
    expect(scheduleToWire(null)).toEqual({});
    expect(scheduleToWire({ hours: ALL_HOURS, days: ALL_DAYS })).toEqual({});
  });
});

describe('scheduleProblem (the engine 422s these at save)', () => {
  it('needs an hour and a day', () => {
    expect(scheduleProblem({ hours: [], days: [0] }, '1h')).toBe('Pick at least one hour.');
    expect(scheduleProblem({ hours: [3], days: [] }, '1h')).toBe('Pick at least one day.');
  });

  it('needs an hour a primary bar opens at: 4h at 0/4/…/20, 1d and 1w at 0', () => {
    expect(scheduleProblem(WEEKDAY_SESSION, '1h')).toBeNull();
    expect(scheduleProblem(WEEKDAY_SESSION, '15m')).toBeNull();
    expect(scheduleProblem(WEEKDAY_SESSION, '4h')).toBeNull(); // 16 and 20 are 4h opens
    expect(scheduleProblem({ hours: [13, 14, 15], days: [0] }, '4h')).toBe(
      '4h bars open only at 0, 4, 8, 12, 16, 20 UTC, so no entry could ever pass: include one of those hours.',
    );
    expect(scheduleProblem(WEEKDAY_SESSION, '1d')).toBe(
      '1d bars open only at 0 UTC, so no entry could ever pass: include one of those hours.',
    );
    expect(scheduleProblem({ hours: [0], days: [0, 1, 2, 3, 4] }, '1d')).toBeNull();
  });

  it('needs Monday on 1w bars', () => {
    expect(scheduleProblem({ hours: [0], days: [1, 2] }, '1w')).toBe('1w bars open on Monday: include Monday.');
    expect(scheduleProblem(null, '1w')).toBeNull();
  });
});

describe('scheduleSummary', () => {
  it('compresses runs and names days Monday-first', () => {
    expect(scheduleSummary({ active_hours_utc: WEEKDAY_SESSION.hours, active_days: WEEKDAY_SESSION.days })).toBe(
      'Entries only on bars opening Mon–Fri, 13–21 UTC',
    );
    expect(scheduleSummary({ active_hours_utc: [0, 1, 23] })).toBe('Entries only on bars opening any day, 00–01, 23 UTC');
    expect(scheduleSummary({ active_days: [5, 6] })).toBe('Entries only on bars opening Sat–Sun, any hour');
    expect(scheduleSummary(null)).toBe('Entries at any time');
  });
});
