# Paper fill rules

How the paper executor turns orders into fills. Backtests use the same executor, so these rules are also the
backtest fill model.

The paper executor (`src/hydra/execution/paper_trading.py`) simulates a Binance spot account; the rules live in
`src/hydra/execution/fill_rules.py`.

- **Spot only.** A SELL needs free base, a BUY needs free USDT for cost plus fee; nothing goes negative or short.
  Resting orders lock what they may spend (a SELL locks base, a BUY locks USDT, an OCO locks once), so a second
  full-size SELL next to a stop is rejected, as on Binance. To exit while a stop rests, cancel it first or call
  `flatten()`. Optional `step_size` / `min_notional` (or per-symbol market rules) reject orders like Binance filters.
- **Fees** are charged in USDT on both sides (maker 0.1% for resting limits, taker 0.1% for market, stop and trailing
  fills by default). This is a deliberate deviation: Binance takes a BUY fee in the base asset unless paid with BNB.
- **Market orders** fill with fixed slippage (default 0.1%) at the latest price set by `set_mark` (the session feeds it
  every `PriceEvent`) since the last closed bar, else at that bar's close. Strategy entries run right after a closed
  bar, so they fill at its close. A market order that would *open* exposure on a mark the feed went stale over is
  refused naming the stale mark — an entry at a stale price is a bet on an outage. Reduce-only exits and `flatten`
  still fill at the stale mark, labelled `stale_live`: an exit at a stale price beats an unexitable position.
- **Volume cap.** No order takes more than `max_participation` (10% by default) of the volume of the bar it fills on
  — `fill_rules.participation_fillable`. An entry market order fills the fillable slice; the uncapped remainder is
  cancelled (`status="CANCELLED"`, reason `participation_cap`, nothing traded) and the create response carries
  `requested_quantity` and `participation_capped`. Exits and flatten are not capped: an exit must never be unfillable.
  Before any closed bar the volume is unknown and market orders fill uncapped. The cap binds only at the thin-book edge
  (rank-90+ alts on 1m bars, large hyperopt capital); it is inert by orders of magnitude on majors.
- **Resting orders slice.** A stop, limit, OCO leg or trailing order fills at most the fillable slice of each bar and
  the remainder keeps working under the same order id; its locked funds shrink with each slice (`_Ledger.consume`)
  and its quantity/locks persist as before. A trailing stop keeps trailing across its slices. A partial OCO leg fill
  drops its sibling and the remainder continues standalone — a documented deviation: Binance cancels the whole list.
  A BUY slice pays from its lock, drawing any gap shortfall from free USDT, and cancels the remainder only when free
  balance and lock together cannot pay the slice. A protective order that partially fills is re-sized in place to the
  remaining position — never re-armed beside its own resting remainder.
- **Closed bars only.** `on_closed_bar(symbol, bar)` evaluates that symbol's resting orders once per bar timestamp
  (older or repeated bars are ignored), then marks the account to the close. An order placed at a live price sees
  its first bar as the move from that price to the close (the bar's earlier range came before the order).
- **Feed gaps.** Bars replayed after a feed gap (catch-up bars) are not evaluated. A stop crossed during the gap fills
  on the first live bar, at the worse of the stop and that bar's open.
- **Intrabar path.** A bullish bar (close > open) is assumed to go open, low, high, close; any other bar open, high, low,
  close. When both OCO legs are touched, the one reached first on that path fills; a level the
  open has already gapped through is reached first.
- **Gaps.** A stop that the bar opens through fills at the open (the worse of stop and open), slipped. A resting limit
  fills at the better of limit and open; that is optimistic for a resting order.
- **Stop-limit.** When the trigger price is already marketable against the limit, it fills at the trigger (taker; the
  limit is only a cap). Otherwise it fills at its limit if the rest of the path reaches it, or becomes a resting limit.
  No fill rule fills outside the bar's low-high range before slippage. **Trailing stops** move their peak (or trough) point by point along the path, so a later
  high never triggers on an earlier low.
- A BUY stop that gaps above its locked amount draws the difference from free USDT, or is cancelled if there is not
  enough; `on_closed_bar` returns that cancellation as a `PaperFill` with `status="CANCELLED"`. Exits never fail on
  balance because their base is locked.
- **Halted account.** A broken ledger invariant or a second writer of the same session halts the executor: it refuses
  new orders, evaluates only resting SELLs, persists nothing more, and `flatten()` still exits.

The backtester runs this executor inside a paper session core, so backtest fills follow every rule above (no live
prices in a backtest: market orders fill at the signal bar's close, with the same volume cap, partial fills and
participation-capped entry remainders a live paper session produces). The runner also replays every declared
non-primary timeframe (confirmation, entry) merged with the primary stream in the live feed's delivery order — by each
bar's close time, the higher timeframe first on ties — so a confirmation bar closing at T is in the context before the
primary bar closing at T is evaluated, and no bar closes in the future of what a decision saw. Backtest results stored
before the volume model are not comparable with new ones.
