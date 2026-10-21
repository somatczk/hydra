/** Number and time formatting (05 §5). Null in gives null out; the caller renders "—" plus a state. */

import { fractionToPercentInput } from './riskLimits';

type Num = number | null | undefined;

const MINUS = '−';
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const fixed = (dp: number) =>
  new Intl.NumberFormat('en-GB', { minimumFractionDigits: dp, maximumFractionDigits: dp });
const DP2 = fixed(2);
const DP5 = fixed(5);
const DP8 = fixed(8);

const ok = (v: Num): v is number => typeof v === 'number' && Number.isFinite(v);

export function money(v: Num): string | null {
  return ok(v) ? DP2.format(v) : null;
}

export const price = money;

/** "+124.10", "−38.62" (U+2212), "0.00": zero (after rounding) carries no sign. */
export function signed(v: Num): string | null {
  if (!ok(v)) return null;
  const abs = DP2.format(Math.abs(v));
  if (abs === DP2.format(0)) return abs;
  return `${v > 0 ? '+' : MINUS}${abs}`;
}

/** Screen-reader text for a signed amount: "gain 124.10 USDT" / "loss 38.62 USDT". */
export function signedLabel(v: Num, unit = 'USDT'): string | null {
  if (!ok(v)) return null;
  const abs = DP2.format(Math.abs(v));
  if (abs === DP2.format(0)) return `${abs} ${unit}`;
  return `${v > 0 ? 'gain' : 'loss'} ${abs} ${unit}`;
}

/** `v` is already a percent (risk routes return ×100). */
export function pct(v: Num, opts: { signed?: boolean } = {}): string | null {
  if (!ok(v)) return null;
  return `${opts.signed ? signed(v) : DP2.format(v)}%`;
}

/** A stored limit fraction (0.005) shown exactly ("0.5%"), never rounded to an integer. */
export function limitPct(fraction: Num): string | null {
  return ok(fraction) ? `${fractionToPercentInput(fraction)}%` : null;
}

/** 5 dp (the BTCUSDT step); a non-zero value below 0.00001 gets 8 dp so it never reads as zero. */
export function qty(v: Num): string | null {
  if (!ok(v)) return null;
  return v !== 0 && Math.abs(v) < 0.00001 ? DP8.format(v) : DP5.format(v);
}

const pad = (n: number) => String(n).padStart(2, '0');

/** "14:02:10 UTC" today, "18 Oct 14:02 UTC" this year, "18 Oct 2025 14:02 UTC" otherwise. */
export function utc(iso: string | number | null | undefined, now: number = Date.now()): string | null {
  if (iso === null || iso === undefined) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const n = new Date(now);
  const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  const sameYear = d.getUTCFullYear() === n.getUTCFullYear();
  if (sameYear && d.getUTCMonth() === n.getUTCMonth() && d.getUTCDate() === n.getUTCDate()) {
    return `${hm}:${pad(d.getUTCSeconds())} UTC`;
  }
  const day = `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
  return sameYear ? `${day} ${hm} UTC` : `${day} ${d.getUTCFullYear()} ${hm} UTC`;
}

const SEC = 1000;
const MIN = 60 * SEC;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

/** "40 s ago", "3 min ago", "2 h ago"; from 24 h on, the absolute UTC time of the event. */
export function age(ms: Num, now: number = Date.now()): string | null {
  if (!ok(ms)) return null;
  const v = Math.max(0, ms);
  if (v < MIN) return `${Math.floor(v / SEC)} s ago`;
  if (v < HOUR) return `${Math.floor(v / MIN)} min ago`;
  if (v < DAY) return `${Math.floor(v / HOUR)} h ago`;
  return utc(now - v, now);
}

/** "40 s", "14 min", "2 h 14 min", "3 d 4 h". */
export function duration(ms: Num): string | null {
  if (!ok(ms)) return null;
  const v = Math.max(0, ms);
  if (v < MIN) return `${Math.floor(v / SEC)} s`;
  if (v < HOUR) return `${Math.floor(v / MIN)} min`;
  if (v < DAY) return `${Math.floor(v / HOUR)} h ${Math.floor((v % HOUR) / MIN)} min`;
  return `${Math.floor(v / DAY)} d ${Math.floor((v % DAY) / HOUR)} h`;
}
