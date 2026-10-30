# Live trading

The operator guide for real-money sessions: the arming ceremony, the exchange keys, the protective-stop
invariant and what to do when an alert fires. What is shipped and what is not is in the README Status table.

## The gate, the keys, the ceremony

Live sessions place real orders on Binance with real money; paper sessions keep running. The gate is model (c):
one deliberate master switch plus a one-time code on every live start and every manual live order.

**The Binance key recipe** (create it in the Binance app before the first arm):

1. **Trade-only**: enable *Enable Spot & Margin Trading*, disable everything else — in particular
   **withdrawals must be off**. Connecting a withdrawals-enabled key on mainnet is refused 422
   `credentials_can_withdraw` with nothing stored, and arming re-fetches the permissions live (R8): a key
   whose withdrawals were enabled since connecting also refuses arming 409 `credentials_not_live_capable`.
2. **IP allowlist recommended, not enforced** (decision: dynamic home egress). Restricting the key to your
   current public IPv4 is the strongest posture, but a rotated ISP address makes keyed calls fail (`-2015`)
   until you update the allowlist and re-validate in Settings; running live sessions keep their in-memory
   clients until restart. Without an allowlist the blast radius of a leaked key is trading your own balance —
   withdrawals are disabled and keys are Fernet-encrypted at rest with `HYDRA_CREDENTIAL_KEY`.
3. **Keys live** in the `exchange_credentials` table (Fernet-encrypted) and are decrypted only to build
   keyed clients; the dashboard's own reads use the read-only client. `validated_permissions` records the
   connect-time permission snapshot (null before P3-C or on testnet = not live-capable).

**The ceremony**: connect the trade-only keys in Settings → `POST /api/live/arm` with the `live_capital`
baseline → type the 6-digit code in the `/activity` review (the same dialog shows the code: this is
transcription friction against shoulder-surfing and fat fingers, not an authentication factor) → `POST
/api/live/session-start` (or the `live_session_start` tool) per live session, each with its own code → manual
live orders via `POST /api/live/order` (or the `live_order` tool), one code per order. Strategy orders inside
a running live session carry **no** per-order hook: the code-gated start IS the authorization for that
strategy's signals, still bounded by the kill switch, the live-scoped risk limits, breakers and the exchange
stops. The codes are single-use, 10-minute expiry, five wrong attempts invalidate, and a different pending
request of the same action answers 409 `approval_conflict`.

**The safe directions stay ungated**: the kill switch, stopping a session, and disarm (`POST /api/live/disarm`)
— disarm's response names every running live session and they keep running under their start's authorization
until you stop them. When live positions are held, follow the [operator checklist](#operator-checklist-hydraenginedown--hydraliveprotectivegap-while-live-sessions-hold-positions)
below: the exchange-side stops keep working even while the engine is down.

**Perps have their own ceremony** (P4-E): arming spot never arms perps. `POST /api/live/perps/arm` with the
`perps_capital` budget issues its own code (`GET /api/live/perps/status`, `POST /api/live/perps/disarm`); the arm
re-fetches the stored futures key's permissions live and refuses unless `enableFutures` reads true. Futures keys
connect through Settings like spot's (`binanceusdm`); on mainnet a key that can withdraw is refused 422
`credentials_can_withdraw` and a key without futures 422 `credentials_not_futures_capable`, with nothing stored.
Unarmed, every live FUTURES surface answers 409 `perps_not_armed` (403 `perps_capital_missing` when armed without the capital).

**Bybit spot is the second live venue, LIVE ONLY** (P7-F): paper sessions, backtests and previews stay
Binance-only — a bybit start of one is refused by name — and a live bybit session runs through exactly the same
armed gate and one-time codes as Binance's.

- **The bybit key recipe**: enable *Spot Trading* on the key, disable everything else — in particular
  **Withdrawal must be off** (bybit names it `Withdraw` inside the key's `Wallet` permission). Connecting a
  withdrawal-capable key on mainnet is refused 422 `credentials_can_withdraw` with nothing stored. The
  permission read is GET `/v5/user/query-api`; arming re-fetches it live for every **connected** supported
  spot venue — an unconnected venue is skipped, so arming binance never waits on bybit, but a connected
  bybit key that has since gained withdrawals refuses the whole arm (fail-closed by design).
- **Bybit has no OCO**: a stop-plus-take-profit is a TP+SL conditional pair behind the spot `tpslOrder`
  filter, the **stop leg placed first**; a filled leg cancels its sibling on both the fill and the reconcile
  path (hydra does the cancelling — bybit has no list to do it). A trailing spec degrades to the fixed stop
  (bybit's trailing order is amount-based, so no percent trail is invented). The kill-switch sweep cancels
  both bybit namespaces (regular orders and `tpslOrder`) per traded symbol.
- **Testnet**: bybit has a real one (`api-testnet.bybit.com`); keys checked there store no permission
  record, like Binance's testnet.
- **Not yet proven on the real exchange**: bybit's live semantics are verified against the wire-contract
  fake (`tests/fixtures/bybit_trading.py`, written from the ccxt source) and the offline ccxt surface
  guard until the on-box testnet battery runs as the first post-deploy action — the perps precedent. The
  battery's first check: a triggered `tpslOrder` keeps the client id (`orderLinkId`) fills are matched on.

**Risk disclosure**: live trading can lose the `live_capital` allocation and more if a stop is rejected or the
exchange fails; the dead-man section states the residual risks. The first real mainnet order is the W8 on-box
battery, testnet first.

## Live protective stops (the dead-man)

Binance spot has **no native kill-switch**: no cancel-all-on-disconnect, no position mode, no account dead-man
switch. Hydra's dead-man is therefore the binding invariant — **no live position without a confirmed exchange-side
protective order** — plus detection and a documented response, not an automated flatten:

- **At placement** (`execution/exchange_stops.py`): every entry fill places its protective order synchronously; if
  the whole ladder fails (`StopUnplaceableError`) the position is market-sold immediately (decision 17). A stop
  already at or above the market (`StopBreached`) market-closes too.
- **At recovery**: a restarted engine re-attaches (`adopt`) hydra's own resting orders and arms a stop for any
  held-but-unprotected position; a held live position with no resting stop gets one.
- **Continuously**: a per-closed-bar coverage check sets/clears `hydra_live_protective_gap`, and
  `HydraLiveProtectiveGap` (critical) pages on any gap. The session detail's positions carry the verified
  `exchange_stop` state (`confirmed` / `missing` / `unknown` at `checked_at`).
- **On futures (P4-D)**: the same binding invariant, with the ladder's own shape — the protective pair is two
  reduce-only conditional orders in the fapi ALGO namespace (`TAKE_PROFIT_MARKET` + `STOP_MARKET`, or one
  `TRAILING_STOP_MARKET`), and BOTH legs must rest for the coverage verify to say `confirmed`. The futures
  dead-man is deliberately NOT a cancel-on-silence: a countdown-cancel or listen-key auto-cancel would strip
  the ladder exactly when a dead engine could not replace it — the opposite of the invariant. When one leg
  fills while the engine is down, its sibling keeps resting (reduce-only, so it can only close the position)
  until the next poller sweep — which the perp reconcile pass runs first — cancels it; the position itself is
  closed by the filled leg either way. The kill-switch sweep cancels both fapi namespaces (regular and ALGO)
  per traded symbol.

### Operator checklist: HydraEngineDown / HydraLiveProtectiveGap while live sessions hold positions

The exchange-side OCOs/stops keep working while the engine is down — that is the point. When the engine-down or
protective-gap alert fires with live positions held:

1. **Do nothing** — the resting stop/OCO on Binance protects the position. This is the default and usually the
   right answer: the stop fills on its own if hit.
2. **Flatten from the Binance app** — if you want out now, sell from the app and cancel the symbol's open orders
   there. The engine reconciles on restart (`reconcile` reports untracked orders as drift, never adopts them).
3. **Restart + Stop all** — restart the engine, then use the shell's **Stop all** (kill switch). Note the sweep's
   blast radius: after per-session flattens it cancels **every** open order on each live-traded symbol, including
   orders hydra did not place on that account.

Residual risks, stated plainly: (a) if the engine is down while a take-profit leg **partially** fills, the
remainder keeps a resting TP but no SL until the engine returns and reconciles — not fixable on Binance spot
today; (b) a SELL stop too far below the average price is rejected by `PERCENT_PRICE_BY_SIDE`, and the ladder's
bottom rung market-closes the position; (c) a live session stopped while it still holds positions keeps
`hydra_live_protective_gap` set for the symbols it holds (all of them when they cannot be read), so the alert keeps
firing until you close the position; a clean flatten clears it.
