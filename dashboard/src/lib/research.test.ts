import { describe, expect, it } from 'vitest';
import { ApiError } from '@/lib/api';
import { unavailable } from '@/test/pageHarness';
import {
  fetchLine,
  holdoutLines,
  jobAgeText,
  jobProgressText,
  jobStatusText,
  jobStatusTone,
  noStoredBars,
  paramSpaceBody,
  stageBadge,
  trialStatusText,
  validationLines,
  wfFoldRow,
  wfWindowPlan,
} from './research';

const noMarketData = (extras: Record<string, unknown> = {}) =>
  new ApiError(
    422,
    JSON.stringify({
      detail: {
        code: 'no_market_data',
        message: 'No market data',
        state: 'no_data',
        exchange: 'binance',
        symbol: 'BTCUSDT',
        timeframe: '1h',
        requested_start: '2026-09-01T00:00:00+00:00',
        requested_end: '2026-10-22T00:00:00+00:00',
        available_first_ts: '2026-09-09T00:00:00+00:00',
        available_last_ts: '2026-10-21T00:00:00+00:00',
        ...extras,
      },
    }),
  );

describe('noStoredBars', () => {
  it('names the market and the stored coverage from the 422 extras', () => {
    expect(noStoredBars(noMarketData())).toEqual({
      title: 'No stored bars for BTCUSDT 1h in this range. Nothing was simulated.',
      line: 'Stored bars cover 9 Sep 2026 to 21 Oct 2026.',
    });
  });

  it('says nothing is stored when the API has no coverage window', () => {
    expect(noStoredBars(noMarketData({ available_first_ts: null, available_last_ts: null }))).toEqual({
      title: 'No stored bars for BTCUSDT 1h in this range. Nothing was simulated.',
      line: 'Nothing is stored for this market yet.',
    });
  });

  it('returns null for anything that is not a no_market_data envelope', () => {
    expect(noStoredBars(unavailable())).toBeNull();
    expect(noStoredBars(new Error('nope'))).toBeNull();
    expect(noStoredBars(null)).toBeNull();
  });
});

describe('jobStatusText', () => {
  it('labels every durable state, cancelling included', () => {
    expect(jobStatusText('queued')).toBe('Queued');
    expect(jobStatusText('running')).toBe('Running');
    expect(jobStatusText('cancelling')).toBe('Cancelling…');
    expect(jobStatusText('completed')).toBe('Completed');
    expect(jobStatusText('failed')).toBe('Failed');
    expect(jobStatusText('cancelled')).toBe('Cancelled');
  });
});

describe('jobStatusTone', () => {
  it('cautions cancelled and cancelling, dangers failed, stays quiet otherwise', () => {
    expect(jobStatusTone('cancelled')).toBe('caution');
    expect(jobStatusTone('cancelling')).toBe('caution');
    expect(jobStatusTone('failed')).toBe('danger');
    expect(jobStatusTone('completed')).toBe('none');
    expect(jobStatusTone('queued')).toBe('none');
    expect(jobStatusTone('running')).toBe('none');
  });
});

describe('jobAgeText', () => {
  const NOW = Date.parse('2026-10-25T12:00:00+00:00');
  const created = '2026-10-25T09:00:00+00:00';
  const finished = '2026-10-25T11:00:00+00:00';

  it('uses finished_at for terminal statuses', () => {
    expect(jobAgeText('completed', created, finished, NOW)).toBe('Finished 1 h ago');
    expect(jobAgeText('failed', created, finished, NOW)).toBe('Finished 1 h ago');
    expect(jobAgeText('cancelled', created, finished, NOW)).toBe('Finished 1 h ago');
  });

  it('falls back to created_at when finished_at is missing', () => {
    expect(jobAgeText('completed', created, null, NOW)).toBe('Finished 3 h ago');
    expect(jobAgeText('cancelled', created, undefined, NOW)).toBe('Finished 3 h ago');
  });

  it('ages active jobs from created_at, whatever finished_at says', () => {
    expect(jobAgeText('running', created, finished, NOW)).toBe('Started 3 h ago');
    expect(jobAgeText('queued', created, finished, NOW)).toBe('Queued 3 h ago');
    expect(jobAgeText('cancelling', created, null, NOW)).toBe('Started 3 h ago');
  });

  it('returns null without a usable timestamp', () => {
    expect(jobAgeText('running', null, null, NOW)).toBeNull();
    expect(jobAgeText('running', 'not-a-date', null, NOW)).toBeNull();
  });
});

describe('jobProgressText', () => {
  it('shows percent for backtests and trial counts for hyperopt', () => {
    expect(jobProgressText('backtest', { progress: 40 })).toBe('40%');
    expect(jobProgressText('backtest', { progress: 42.6 })).toBe('43%');
    expect(jobProgressText('hyperopt', { completed_trials: 12, total_trials: 100 })).toBe('12/100 trials');
  });

  it('is null for walk-forward, and for missing numbers', () => {
    expect(jobProgressText('walkforward', { progress: 40 })).toBeNull();
    expect(jobProgressText('backtest', {})).toBeNull();
    expect(jobProgressText('hyperopt', { completed_trials: 3 })).toBeNull();
  });
});

describe('holdoutLines', () => {
  const windows = { holdout_start: '2026-09-09T00:00:00+00:00', holdout_end: '2026-10-21T00:00:00+00:00' };

  it('is null when the run carved no holdout', () => {
    expect(holdoutLines(null, windows)).toBeNull();
  });

  it('describes the window and the metrics', () => {
    expect(
      holdoutLines({ sharpe: 1.423, total_return: 0.1234, max_drawdown: 0.041, total_trades: 38 }, windows),
    ).toEqual([
      'Holdout window: 9 Sep 2026 to 21 Oct 2026',
      'Sharpe 1.42',
      'Return 12.34%',
      'Max drawdown 4.10%',
      'Trades 38',
    ]);
  });

  it('skips the window line when the API sent no holdout dates', () => {
    expect(
      holdoutLines({ sharpe: 0, total_return: 0, max_drawdown: 0, total_trades: 0 }, { holdout_start: null, holdout_end: null }),
    ).toEqual(['Sharpe 0.00', 'Return 0.00%', 'Max drawdown 0.00%', 'Trades 0']);
  });
});

describe('validationLines', () => {
  it('is null when the API returned none of them (never 0-from-null)', () => {
    expect(validationLines({ holdout_psr: null, dsr: null, pbo: null })).toBeNull();
    expect(validationLines({})).toBeNull();
  });

  it('renders only the values the API returned', () => {
    expect(validationLines({ pbo: 0.3, dsr: 0.41 })).toBe('PBO 0.30 · DSR 0.41');
    expect(validationLines({ holdout_psr: 0.62, dsr: 0.41, pbo: 0.3 })).toBe(
      'PBO 0.30 · DSR 0.41 · Holdout PSR 0.62',
    );
    expect(validationLines({ holdout_psr: 0.62 })).toBe('Holdout PSR 0.62');
  });
});

describe('trialStatusText', () => {
  it('labels failed and no-trade trials; a healthy trial needs no label', () => {
    expect(trialStatusText('failed')).toBe('Failed');
    expect(trialStatusText('no_trades')).toBe('No trades');
    expect(trialStatusText('ok')).toBeNull();
    expect(trialStatusText(null)).toBeNull();
  });
});

describe('paramSpaceBody', () => {
  it('maps params to the wire shape without the step key', () => {
    const body = paramSpaceBody([
      { name: 'period', label: 'Period', type: 'int', default: 20, min: 2, max: 80, step: 1 },
      { name: 'threshold', label: 'Threshold', type: 'float', default: 0.5, min: 0.1, max: 2, step: 0.1 },
    ]);
    expect(body).toEqual([
      { name: 'period', type: 'int', low: 2, high: 80 },
      { name: 'threshold', type: 'float', low: 0.1, high: 2 },
    ]);
    expect('step' in body[0]).toBe(false);
    expect('default' in body[0]).toBe(false);
  });
});

describe('wfFoldRow', () => {
  it('renders the two windows and the selected params', () => {
    expect(
      wfFoldRow({
        fold_index: 1,
        is_start: '2026-01-01T00:00:00+00:00',
        is_end: '2026-03-31T00:00:00+00:00',
        oos_start: '2026-04-01T00:00:00+00:00',
        oos_end: '2026-05-31T00:00:00+00:00',
        selected_params: { period: 42 },
        n_trials: 20,
      }),
    ).toEqual({
      isWindow: '1 Jan 2026 to 31 Mar 2026',
      oosWindow: '1 Apr 2026 to 31 May 2026',
      params: 'period=42',
    });
  });

  it('joins several params and falls back to em dashes when the fold carries none', () => {
    expect(wfFoldRow({ fold_index: 2, selected_params: { period: 42, threshold: 0.5 } }).params).toBe(
      'period=42, threshold=0.5',
    );
    expect(wfFoldRow({ fold_index: 3 })).toEqual({ isWindow: '—', oosWindow: '—', params: '—' });
  });
});

describe('wfWindowPlan', () => {
  it('tiles the span with one train window and exactly k test windows, like fold_windows', () => {
    // 424 days at 6 folds, 75 %: test = floor(424 · 0.25 / 2.25) = 47, train = 424 − 6 · 47 = 142
    expect(wfWindowPlan('2025-01-01', '2026-03-01', 6, 0.75)).toEqual({ isDays: 142, oosDays: 47 });
    expect(wfWindowPlan('2025-01-01', '2026-03-01', 2, 0.75)).toEqual({ isDays: 256, oosDays: 84 });
    expect(wfWindowPlan('2023-01-01', '2026-01-01', 12, 0.75)).toEqual({ isDays: 220, oosDays: 73 });
  });

  it('keeps the train/test ratio up to the floor remainder', () => {
    // 609 days at 4 folds, 50 %: test = floor(304.5 / 2.5) = 121, train = 609 − 484 = 125
    expect(wfWindowPlan('2024-01-01', '2025-09-01', 4, 0.5)).toEqual({ isDays: 125, oosDays: 121 });
  });

  it('is null when a window is under 30 days or an input is unusable', () => {
    expect(wfWindowPlan('2026-01-01', '2026-02-15', 2, 0.75)).toBeNull();
    expect(wfWindowPlan('2025-01-01', '2025-05-01', 6, 0.75)).toBeNull();
    expect(wfWindowPlan('2026-03-01', '2025-01-01', 6, 0.75)).toBeNull();
    expect(wfWindowPlan('2025-01-01', '2026-03-01', 6, 1)).toBeNull();
    expect(wfWindowPlan('', '2026-03-01', 6, 0.75)).toBeNull();
  });
});

describe('fetchLine', () => {
  const RANGES = [['2026-01-01T00:00:00+00:00', '2026-01-31T23:00:00+00:00']];

  it('says what is being fetched while the worker fetches', () => {
    expect(fetchLine({ state: 'fetching', ranges: RANGES, bars_fetched: 300, weight_spent: 6, elapsed_ms: 4200 })).toBe(
      'Fetching missing bars: 300 so far over 1 range (1 Jan 2026 – 31 Jan 2026) · weight 6 · 4 s',
    );
  });

  it('records what was fetched once the run went ahead, and caps the ranges at three', () => {
    const ranges = [
      ['2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00'],
      ['2026-02-01T00:00:00+00:00', '2026-02-02T00:00:00+00:00'],
      ['2026-03-01T00:00:00+00:00', '2026-03-02T00:00:00+00:00'],
      ['2026-04-01T00:00:00+00:00', '2026-04-02T00:00:00+00:00'],
    ];
    expect(fetchLine({ state: 'fetched', ranges, bars_fetched: 1234, weight_spent: 40, elapsed_ms: 75_000 })).toBe(
      'Fetched 1,234 missing bars over 4 ranges (1 Jan 2026 – 2 Jan 2026; 1 Feb 2026 – 2 Feb 2026; 1 Mar 2026 – 2 Mar 2026; 1 more) before the run · weight 40 · 1 min',
    );
    expect(fetchLine({ state: 'fetched', ranges: [], bars_fetched: 0, weight_spent: 0, elapsed_ms: 3 })).toBe(
      'No missing bars: every bar was already stored',
    );
  });

  it('says the job waits while another worker fetches the same dataset', () => {
    expect(fetchLine({ state: 'waiting', ranges: [], bars_fetched: 0, weight_spent: 0, elapsed_ms: 2 })).toBe(
      "Waiting for another worker's fetch of these bars to finish",
    );
  });

  it('names the failure reason, including a failure before any progress (state and reason only)', () => {
    expect(fetchLine({ state: 'failed', reason: 'rate_limited' })).toBe('Fetching missing bars failed (rate_limited) after 0 bars');
    expect(fetchLine(null)).toBeNull();
  });
});

describe('stageBadge', () => {
  it('reads either casing of a registry stage as one badge', () => {
    expect(stageBadge('Production')).toEqual({ label: 'Production', variant: 'success' });
    expect(stageBadge('production')).toEqual({ label: 'Production', variant: 'success' });
    expect(stageBadge('staging')).toEqual({ label: 'Staging', variant: 'info' });
    expect(stageBadge('Archived')).toEqual({ label: 'Archived', variant: 'neutral' });
  });
});
