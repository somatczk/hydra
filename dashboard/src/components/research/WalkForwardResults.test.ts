import { describe, expect, it } from 'vitest';
import {
  foldTicks,
  type WalkForwardResult,
  type WfFoldMetrics,
} from '@/components/research/WalkForwardResults';

function fold(index: number, oosStart: string, oosEnd: string): WfFoldMetrics {
  return {
    fold_index: index,
    in_sample_sharpe: 1.2,
    in_sample_return: 0.08,
    in_sample_drawdown: 0.03,
    in_sample_trades: 12,
    out_of_sample_sharpe: 0.9,
    out_of_sample_return: 0.04,
    out_of_sample_drawdown: 0.05,
    out_of_sample_trades: 9,
    selected_params: null,
    n_trials: 7,
    oos_start: oosStart,
    oos_end: oosEnd,
  };
}

/** 4 equity points over 2 folds: fold 1's start tick and the last index both land on 3. */
function result(): WalkForwardResult {
  return {
    task_id: 'wf-1',
    status: 'completed',
    folds: [
      fold(0, '2026-01-01T00:00:00+00:00', '2026-02-01T00:00:00+00:00'),
      fold(1, '2026-03-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00'),
    ],
    aggregated_sharpe: null,
    aggregated_return: null,
    aggregated_drawdown: null,
    out_of_sample_equity: [10000, 10100, 10200, 10300],
    oos_psr: null,
    efficiency: null,
    anchored: false,
    seed: 1,
  };
}

describe('foldTicks', () => {
  it('keeps both fold start labels when the last fold lands on the last index', () => {
    const { ticks, label } = foldTicks(result());

    expect(ticks).toEqual([1, 3]);
    expect(label(1)).toBe('1 Jan 2026');
    expect(label(3)).toBe('1 Mar 2026');
  });
});
