'use client';

import { useEffect, useRef, useState } from 'react';
import { LiveOrderReview } from '@/components/live/LiveOrderReview';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { cn } from '@/components/ui/cn';
import { fetchApi } from '@/lib/api';
import { isPerpSession, needsLiveOrderReview, sessionName, stopRuleLabel, type PositionItem, type SessionDetail, type StopRule } from '@/lib/derive';
import { money } from '@/lib/format';
import { baseAsset, PERP_MANUAL_LONG_ONLY, quickTradeOutcome, type Outcome, type QuickTradeRequest, type QuickTradeResponse } from '@/lib/outcomes';
import { settle } from './useOutcomeSlot';

type Side = 'buy' | 'sell';
type OrderType = 'MARKET' | 'LIMIT';
type Protection = 'default' | 'stop' | 'trailing';

const DECIMAL = /^\d+(\.\d{1,8})?$/;
const positive = (v: string) => DECIMAL.test(v) && Number(v) > 0;
const sameSymbol = (a: string, b: string) => a.replace('/', '') === b.replace('/', '');

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string; disabled?: boolean }>;
  onChange: (v: T) => void;
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex gap-2">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          disabled={o.disabled}
          onClick={() => onChange(o.value)}
          className={cn(
            'h-10 rounded border px-4 text-sm font-medium max-lg:h-11 max-lg:min-w-11',
            value === o.value ? 'border-accent bg-hover text-ink-1' : 'border-line-strong text-ink-2 hover:bg-hover',
            o.disabled && 'cursor-not-allowed opacity-50 hover:bg-transparent',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

interface QuickTradeProps {
  session: SessionDetail;
  positions: PositionItem[];
  /** A result that belongs in the page's outcome slot. */
  onOutcome: (outcome: Outcome) => void;
  /** After any request that reached the engine, so the page re-reads positions and fills. */
  onSent: () => void;
}

/** A manual order in one running session (F5): live summary above a single primary. A running live
 * session swaps the primary for the L4 review step (08 §6); paper keeps the single-step flow. */
export function QuickTrade({ session, positions, onOutcome, onSent }: QuickTradeProps) {
  const symbol = session.symbols[0] ?? '';
  const base = baseAsset(symbol);
  const name = sessionName(session);
  const live = needsLiveOrderReview(session);
  const [side, setSide] = useState<Side>('buy');
  const [quantity, setQuantity] = useState('');
  const [orderType, setOrderType] = useState<OrderType>('MARKET');
  const [limitPrice, setLimitPrice] = useState('');
  const [protection, setProtection] = useState<Protection>('default');
  const [stopLoss, setStopLoss] = useState('');
  const [takeProfit, setTakeProfit] = useState('');
  const [trailing, setTrailing] = useState('');
  const [pending, setPending] = useState(false);
  const [inline, setInline] = useState<string | null>(null);
  const [rule, setRule] = useState<StopRule | Error | null>(null);
  const [review, setReview] = useState<QuickTradeRequest | null>(null);
  const sending = useRef(false);

  useEffect(() => {
    let alive = true;
    fetchApi<{ risk: StopRule }>(`/api/strategies/${session.strategy_id}`)
      .then((s) => alive && setRule(s.risk))
      .catch((e: unknown) => alive && setRule(e instanceof Error ? e : new Error(String(e))));
    return () => {
      alive = false;
    };
  }, [session.strategy_id]);

  const held = positions.filter((p) => sameSymbol(p.symbol, symbol)).reduce((n, p) => n + p.quantity, 0);
  const shortHeld = isPerpSession(session) && positions.some((p) => sameSymbol(p.symbol, symbol) && p.direction === 'short');
  const longHeld = positions.filter((p) => sameSymbol(p.symbol, symbol) && p.direction !== 'short').reduce((n, p) => n + p.quantity, 0);
  const buy = side === 'buy';
  // §4(2): manual/quick-trade on perps is LONG-ONLY in 2.8.0 — the SELL that would open a SHORT
  // entry is refused up front with the cut named, and so is the BUY over a strategy-held short.
  const perpSellCut = isPerpSession(session) && longHeld <= 0
    ? `No ${base} long held: ${PERP_MANUAL_LONG_ONLY} (use a strategy session to trade shorts).`
    : null;
  const perpShortBuyCut = isPerpSession(session) && shortHeld
    ? `${symbol} is held short by the strategy: ${PERP_MANUAL_LONG_ONLY} — its own exit rules and the kill switch close it (BUY-back).`
    : null;
  const errors = {
    side: buy ? perpShortBuyCut : (perpSellCut ?? (!buy && held <= 0 ? "Nothing to sell. Spot can't go short." : null)),
    quantity: quantity !== '' && !positive(quantity) ? 'Enter a quantity above 0 with up to 8 decimals.' : null,
    price:
      orderType !== 'LIMIT'
        ? null
        : limitPrice === ''
          ? 'A limit order needs a price.'
          : positive(limitPrice)
            ? null
            : 'Enter a price above 0 with up to 8 decimals.',
    stop: !buy || protection !== 'stop' ? null : stopLoss === '' || !positive(stopLoss) ? 'Enter a stop price above 0 with up to 8 decimals.' : null,
    takeProfit: !buy || protection !== 'stop' || takeProfit === '' ? null : !positive(takeProfit) ? 'Enter a take-profit price above 0 with up to 8 decimals.' : null,
    order:
      buy && protection === 'stop' && positive(stopLoss) && positive(takeProfit) && Number(stopLoss) >= Number(takeProfit)
        ? 'Stop price must be below take-profit price.'
        : null,
    trailing:
      !buy || protection !== 'trailing' ? null : DECIMAL.test(trailing) && Number(trailing) > 0 && Number(trailing) < 100 ? null : 'Trailing stop must be above 0 and below 100.',
  };
  const valid = positive(quantity) && Object.values(errors).every((e) => e === null);

  const markValue = positions.length > 0 && held > 0
    ? positions.filter((p) => sameSymbol(p.symbol, symbol)).reduce((n, p) => n + p.quantity * p.avg_entry_price + p.unrealized_pnl, 0) / held
    : null;
  const unit = orderType === 'LIMIT' && positive(limitPrice) ? Number(limitPrice) : markValue;
  const about =
    unit === null
      ? 'Value unknown until it fills.'
      : `About ${money(Number(quantity) * unit)} USDT at ${orderType === 'LIMIT' ? 'the limit price' : "the last closed bar's price"}.`;
  const ruleText = rule === null ? 'reading the rule…' : stopRuleLabel(rule);
  const stopLine = !buy
    ? null
    : protection === 'stop'
      ? `Stop at ${stopLoss}${takeProfit ? `, take-profit at ${takeProfit}` : ''}.`
      : protection === 'trailing'
        ? `Trailing stop ${trailing}%.`
        : `Strategy default stop (${ruleText}).`;
  const verb = buy ? 'Buy' : 'Sell';

  const buildReq = (): QuickTradeRequest => ({
    symbol,
    side,
    order_type: orderType,
    quantity,
    session_id: session.session_id,
    ...(orderType === 'LIMIT' ? { price: limitPrice } : {}),
    ...(buy && protection === 'stop' ? { stop_loss: stopLoss, ...(takeProfit ? { take_profit: takeProfit } : {}) } : {}),
    ...(buy && protection === 'trailing' ? { trailing_stop_pct: trailing } : {}),
  });

  const submit = async () => {
    if (!valid || sending.current) return;
    if (live) {
      setReview(buildReq());
      return;
    }
    sending.current = true;
    setPending(true);
    setInline(null);
    const req: QuickTradeRequest = buildReq();
    const result = await settle(fetchApi<QuickTradeResponse>('/api/trading/quick-trade', { method: 'POST', body: JSON.stringify(req) }));
    const outcome = quickTradeOutcome(req, result, session.session_id);
    if (outcome.inline) setInline(outcome.title);
    else onOutcome(outcome);
    if ('ok' in result) setQuantity('');
    sending.current = false;
    setPending(false);
    onSent();
  };

  if (live && review) {
    return (
      <LiveOrderReview
        session={session}
        req={review}
        about={about}
        onOutcome={(o) => {
          setReview(null);
          onOutcome(o);
        }}
        onPlaced={() => setQuantity('')}
        onSent={onSent}
        onBack={() => setReview(null)}
      />
    );
  }

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className="flex flex-col gap-1.5">
        <Segmented
          label="Side"
          value={side}
          options={[
            { value: 'buy', label: 'Buy', disabled: perpShortBuyCut !== null },
            // §4(2): the visibly-disabled SHORT side on perps — manual orders are long-only in 2.8.0.
            { value: 'sell', label: 'Sell', disabled: perpSellCut !== null },
          ]}
          onChange={setSide}
        />
        {/* The disabled side's reason stands visible without clicking it (never half-broken). */}
        {errors.side ? (
          <p role="alert" className="text-xs text-danger">
            {errors.side}
          </p>
        ) : perpSellCut ? (
          <p className="text-xs text-danger">{perpSellCut}</p>
        ) : null}
      </div>
      <Input
        label={`Quantity (${base})`}
        inputMode="decimal"
        autoComplete="off"
        value={quantity}
        onChange={(e) => setQuantity(e.target.value.trim())}
        error={errors.quantity ?? undefined}
        hint="Up to 8 decimals"
      />
      <Segmented
        label="Order type"
        value={orderType}
        options={[
          { value: 'MARKET', label: 'Market' },
          { value: 'LIMIT', label: 'Limit' },
        ]}
        onChange={setOrderType}
      />
      {orderType === 'LIMIT' && (
        <Input
          label="Limit price"
          inputMode="decimal"
          autoComplete="off"
          value={limitPrice}
          onChange={(e) => setLimitPrice(e.target.value.trim())}
          error={errors.price ?? undefined}
        />
      )}
      {buy && (
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-medium text-ink-2">Protection</legend>
          {(
            [
              ['default', `Strategy default stop (${ruleText})`],
              ['stop', 'Stop price'],
              ['trailing', 'Trailing stop (%)'],
            ] as const
          ).map(([value, label]) => (
            <label key={value} className="flex min-h-8 items-center gap-2 text-sm max-lg:min-h-11">
              <input type="radio" name="protection" checked={protection === value} onChange={() => setProtection(value)} />
              {label}
            </label>
          ))}
          {protection === 'stop' && (
            <>
              <Input
                label="Stop price (USDT)"
                inputMode="decimal"
                autoComplete="off"
                value={stopLoss}
                onChange={(e) => setStopLoss(e.target.value.trim())}
                error={errors.stop ?? undefined}
              />
              <Input
                label="Take-profit price (optional)"
                inputMode="decimal"
                autoComplete="off"
                value={takeProfit}
                onChange={(e) => setTakeProfit(e.target.value.trim())}
                error={errors.takeProfit ?? errors.order ?? undefined}
              />
            </>
          )}
          {protection === 'trailing' && (
            <Input
              label="Trailing distance (%)"
              inputMode="decimal"
              autoComplete="off"
              value={trailing}
              onChange={(e) => setTrailing(e.target.value.trim())}
              error={errors.trailing ?? undefined}
            />
          )}
        </fieldset>
      )}
      {positive(quantity) && (
        <p aria-live="off" className="rounded border border-line bg-canvas p-3 text-sm">
          <b>
            {verb} {quantity} {base} at {orderType === 'LIMIT' && positive(limitPrice) ? limitPrice : 'market'} ({live ? 'live' : 'paper'}).
          </b>{' '}
          {about} {stopLine} Session {name}.
        </p>
      )}
      <Button type="submit" size="lg" disabled={!valid} loading={pending} className="max-lg:w-full">
        {pending ? 'Placing order…' : live ? 'Review live order' : [verb, positive(quantity) ? quantity : null, base, '(paper)'].filter(Boolean).join(' ')}
      </Button>
      {inline && (
        <p role="alert" className="text-sm text-danger">
          {inline}
        </p>
      )}
    </form>
  );
}
