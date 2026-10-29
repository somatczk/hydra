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
  bar, so they fill at its close.
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
prices in a backtest: market orders fill at the signal bar's close).
