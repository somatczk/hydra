import type { OpenOrder } from '@/lib/derive';
import { limitPct, price, qty } from '@/lib/format';
import { baseAsset } from '@/lib/outcomes';

/** P7-G: one resting perp protective order as a unit. Futures has no OCO, so a stop plus
 * take-profit is two reduce-only legs with sibling-cancel — both legs share `oco_id`
 * (`perp_trading._create_oco`) and render as ONE pair unit, never two unrelated rows. */
export type PerpProtectiveUnit =
  | { kind: 'pair'; id: string; symbol: string; side: string; quantity: number; takeProfit: number; stop: number; reduceOnly: boolean }
  | {
      kind: 'single';
      id: string;
      symbol: string;
      side: string;
      quantity: number;
      stop: number;
      reason: string;
      trailPct: number | null;
      reduceOnly: boolean;
    };

function singleLeg(o: OpenOrder): PerpProtectiveUnit {
  return {
    kind: 'single',
    id: o.id,
    symbol: o.symbol,
    side: o.side,
    quantity: o.amount,
    stop: o.stop_price as number,
    reason: o.reason,
    trailPct: o.trail_pct ?? null,
    reduceOnly: o.reduce_only === true,
  };
}

/** Groups a paper perp session's resting protective orders into units, payload order kept.
 * A pair whose legs don't pair up (a sibling already gone, or reasons out of vocabulary)
 * renders each leg as itself — the honest leftover, never a guessed pair. */
export function perpProtectiveUnits(orders: OpenOrder[]): PerpProtectiveUnit[] {
  const units: PerpProtectiveUnit[] = [];
  const seen = new Set<string>();
  for (const o of orders) {
    if (o.protective !== true || seen.has(o.id)) continue;
    if (!o.oco_id) {
      if (o.stop_price === null) continue;
      seen.add(o.id);
      units.push(singleLeg(o));
      continue;
    }
    const legs = orders.filter((x) => x.oco_id === o.oco_id);
    for (const l of legs) seen.add(l.id);
    const tp = legs.find((l) => l.reason === 'take_profit' && l.stop_price !== null);
    const sl = legs.find((l) => l.reason === 'stop_loss' && l.stop_price !== null);
    if (tp && sl) {
      units.push({
        kind: 'pair',
        id: o.oco_id,
        symbol: sl.symbol,
        side: sl.side,
        quantity: sl.amount,
        takeProfit: tp.stop_price as number,
        stop: sl.stop_price as number,
        reduceOnly: legs.every((l) => l.reduce_only === true),
      });
    } else {
      for (const l of legs) if (l.stop_price !== null) units.push(singleLeg(l));
    }
  }
  return units;
}

interface PerpProtectionProps {
  orders: OpenOrder[];
}

/** P7-G: where a paper perp session's resting protective orders list, each group renders
 * as ONE named unit — the NO-OCO pair as a two-leg card with both prices, a trailing
 * group with its trail note. Live perp sessions read their stops through the A8 exchange
 * verify instead (their exchange legs carry no pair id in the payload). */
export function PerpProtection({ orders }: PerpProtectionProps) {
  const units = perpProtectiveUnits(orders);
  if (units.length === 0) return null;
  return (
    <div className="mt-3 flex flex-col gap-2">
      <h3 className="text-sm font-medium text-ink-2">Protection</h3>
      {units.map((u) => {
        const amount = `${qty(u.quantity)} ${baseAsset(u.symbol)} (${u.side})`;
        return (
          <div key={u.id} className="rounded border border-line bg-surface p-3">
            {u.kind === 'pair' ? (
              <>
                <p className="text-sm font-semibold text-ink-1">Protective pair — one two-leg unit, no OCO on futures</p>
                <p className="mt-1 text-sm text-ink-1">
                  Take-profit {price(u.takeProfit)} · Stop {price(u.stop)} · {amount}
                  {u.reduceOnly ? ' · reduce-only — one leg fills and the other cancels' : ''}
                </p>
              </>
            ) : u.trailPct !== null ? (
              <p className="text-sm text-ink-1">
                Trailing stop {limitPct(u.trailPct)} · stop at {price(u.stop)} · {amount}
              </p>
            ) : (
              <p className="text-sm text-ink-1">
                {u.reason === 'take_profit' ? 'Take-profit at' : 'Stop at'} {price(u.stop)} · {amount}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}
