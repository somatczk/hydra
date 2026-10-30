"""The live session reconcile: the drift detector between the sleeve, its rows and the exchange.

``reconcile_live_session`` never trusts one side blindly and never fixes the ledger by
decree (R5): an exchange order hydra does not know is reported as informational drift —
never adopted, so flatten never trades what the session does not own — a terminal status
is applied only from positive proof (a fetch_order answer, or every OCO leg answering),
and every unsettled row resolves through A1's own path (the poller's sweep: the
N-consecutive/60s not-found window and the myTrades cross-check live THERE, never here —
no second, divergent window). Fills are the poller's claim-apply-confirm pipeline, called
through the executor, never re-applied by hand.

The state machine is fail-closed: any read the pass depends on — the exchange, the
live-order store or the executor — failing answers ``unknown`` — never ``ok``, never
``mismatch``. Any finding (an untracked order, an
unresolved submit past its resolve grace, a proven terminal applied to a row we still
held open, balance drift) answers ``mismatch`` with the findings composed into one detail
line; a clean pass answers ``ok`` with no detail. Before the first pass the holder reports
``never``.

Only exchange-reported facts mutate state, and only through the store's monotonic
``apply_status``; a refused or unchanged apply is a benign no-op (R16), never a finding.
An exchange leg tracked by ANY session's ``live_orders`` row is tracked-elsewhere and
skipped (A1: the classification is account-wide; the fill pipeline's lookups stay
session-scoped, F1) — only a leg no session knows is ``untracked`` (R5: never adopted).
Balance drift compares the sleeve's own claim per asset against the wallet TOTAL
(free + locked): a shared wallet, the user's own resting orders on any symbol and
Earn-style locks may legitimately hold more than the sleeve — Q3 tracks only the
session's own fills, so a surplus is not drift — but the exchange holding less than the
sleeve's claim is. It is reported, never auto-rebased.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from hydra.core.types import OrderType
from hydra.execution.live_executor import LiveOrderPoller, LiveTradingExecutor
from hydra.execution.live_orders import (
    TERMINAL_STATUSES,
    LiveOrderRecord,
    LiveOrderStatus,
    LiveOrderStore,
    map_ccxt_status,
)
from hydra.execution.paper_trading import _opt_d
from hydra.execution.venue_stops import fetch_bybit_leg, is_pair_venue, normalize_open_orders

logger = logging.getLogger(__name__)

ReconcileState = Literal["ok", "mismatch", "unknown", "never"]

# dust and the BNB-commission approximation stay under 0.1 % of the sleeve's balance
BALANCE_DRIFT_THRESHOLD = Decimal("0.001")

COVERING_STATES = frozenset({"ok", "mismatch"})


class _OrderReadError(Exception):
    """An exchange order read failed in a way that is not evidence (a network error)."""


@dataclass(frozen=True, slots=True)
class ReconcileStatus:
    """One live session's reconcile state; the A7 read serializes ``state``,
    ``last_reconciled_at`` and ``detail`` verbatim.

    ``covered_at`` is R20's boundary: the time of the LAST pass whose state was ``ok``
    or ``mismatch``. A fill is reconciled when its timestamp is at or before it — an
    ``unknown`` pass (a failed read) verifies nothing, so it never covers.
    """

    state: ReconcileState
    last_reconciled_at: datetime | None = None
    detail: str | None = None
    covered_at: datetime | None = None


NEVER_STATUS = ReconcileStatus(state="never")


def _status(
    state: ReconcileState,
    ran_at: datetime,
    detail: str | None,
    previous: ReconcileStatus | None,
) -> ReconcileStatus:
    covered_at = None if previous is None else previous.covered_at
    if state in COVERING_STATES:
        covered_at = ran_at
    return ReconcileStatus(
        state=state, last_reconciled_at=ran_at, detail=detail, covered_at=covered_at
    )


async def reconcile_live_session(
    store: LiveOrderStore,
    client: Any,
    executor: LiveTradingExecutor,
    *,
    poller: LiveOrderPoller | None = None,
    previous: ReconcileStatus | None = None,
    now: datetime | None = None,
) -> ReconcileStatus:
    """One reconcile pass; returns the new :class:`ReconcileStatus` for the holder.

    *store* is the session's live-order store, *client* its keyed exchange client and
    *executor* its live executor; *poller*, when wired, is A1's resolution path — the
    pass triggers its sweep, so unsettled submits resolve and fills land through exactly
    the pipeline the 5-second loop uses. Without a poller the pass still collects fills
    through the executor (idempotent by the live_fills constraint) and only reports.
    """
    session_id = executor._session_id
    ran_at = datetime.now(UTC) if now is None else now
    # (d) + (e): A1's own resolution and fill pipeline, never a second window
    try:
        if poller is not None:
            await poller.sweep()
        else:
            await executor.collect_exchange_fills(None)
    except Exception as exc:
        logger.warning("Reconcile sweep of %s failed: %s", session_id, exc)
        return _status("unknown", ran_at, f"resolution and fill sweep failed: {exc}", previous)

    try:
        return await _drift_pass(store, client, executor, session_id, ran_at, previous)
    except Exception as exc:
        # A4≡S5: a failed dependent read is unknown — never a latched stale ok
        logger.warning("Reconcile of %s failed on a dependent read: %s", session_id, exc)
        return _status("unknown", ran_at, f"dependent read failed: {exc}", previous)


async def _drift_pass(
    store: LiveOrderStore,
    client: Any,
    executor: LiveTradingExecutor,
    session_id: str,
    ran_at: datetime,
    previous: ReconcileStatus | None,
) -> ReconcileStatus:
    """(a)-(f): the comparison pass; every read it depends on may fail into ``unknown``."""
    # (a) the exchange's open orders in the one normalized shape, per tracked symbol
    symbols = sorted(
        set(await store.symbols(session_id))
        | {str(p.symbol) for p in await executor.get_positions() if p.quantity > 0}
    )
    pair_venue = is_pair_venue(str(executor._exchange_id))
    legs: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            raw = await client.fetch_open_orders(symbol)
            if pair_venue:
                # bybit's conditional legs rest behind the tpslOrder filter: read it too
                raw += await client.fetch_open_orders(symbol, conditional=True)
            legs.extend(normalize_open_orders(str(executor._exchange_id), raw))
        except Exception as exc:
            logger.warning("Reconcile open orders of %s unreadable: %s", symbol, exc)
            return _status(
                "unknown", ran_at, f"open orders of {symbol} unreadable: {exc}", previous
            )

    open_rows = await store.open_orders(session_id)
    findings: list[str] = []

    # (b) an exchange order no session knows is reported, never adopted (R5)
    for leg in legs:
        if not await _tracked(store, leg):
            findings.append(f"untracked order {leg['id']} on {leg['symbol']} (not managed)")

    # (c) a row we hold open that the exchange no longer lists: prove how it ended
    try:
        for row in open_rows:
            if any(_leg_matches_row(leg, row) for leg in legs):
                continue
            finding = await _terminalize(store, client, row, pair_venue=pair_venue)
            if finding is not None:
                findings.append(finding)
    except _OrderReadError as exc:
        logger.warning("Reconcile of %s could not read an order: %s", session_id, exc)
        return _status("unknown", ran_at, f"order status unreadable: {exc}", previous)

    # (d) what is still unsettled past the resolve grace is honestly unresolved
    deadline = ran_at - timedelta(seconds=executor.resolve_grace)
    for row in await store.unsettled(session_id, deadline):
        findings.append(f"unresolved submit {row.client_order_id}")

    # (f) the sleeve's own claim per asset against the wallet total (free + locked)
    try:
        wallet = await client.fetch_balance_totals()
    except Exception as exc:
        logger.warning("Reconcile balance of %s unreadable: %s", session_id, exc)
        return _status("unknown", ran_at, f"exchange balance unreadable: {exc}", previous)
    sleeve = await executor.fetch_balance()
    findings.extend(_balance_findings(sleeve, wallet))

    if findings:
        return _status("mismatch", ran_at, "; ".join(findings), previous)
    return _status("ok", ran_at, None, previous)


# ---------------------------------------------------------------------------
# (a)/(b): exchange legs vs the tracked rows
# ---------------------------------------------------------------------------


async def _tracked(store: LiveOrderStore, leg: dict[str, Any]) -> bool:
    """Is this exchange leg tracked by ANY session's live_orders row (A1, account-wide)?

    Another session's tracked order is tracked-elsewhere — skipped, never ``untracked``
    here — while the fill pipeline's lookups stay session-scoped (F1).
    """
    leg_id = str(leg.get("id") or "")
    if leg_id and await store.tracked_by_exchange_order_id(leg_id):
        return True
    oco_id = leg.get("oco_id")
    if oco_id and await store.tracked_by_order_list_id(str(oco_id)):
        return True
    client_id = str(leg.get("client_order_id") or "")
    if not client_id:
        return False
    if await store.by_client_order_id(client_id) is not None:
        return True
    return await store.tracked_by_leg_client_order_id(client_id)


def _leg_matches_row(leg: dict[str, Any], row: LiveOrderRecord) -> bool:
    """Does this exchange leg belong to this DB row (by exchange id, list id or client id)?"""
    client_id = str(leg.get("client_order_id") or "")
    if client_id and (client_id == row.client_order_id or client_id in row.leg_client_order_ids):
        return True
    leg_id = str(leg.get("id") or "")
    if leg_id and row.exchange_order_id is not None and leg_id == row.exchange_order_id:
        return True
    oco_id = leg.get("oco_id")
    return bool(
        oco_id and row.exchange_order_id is not None and str(oco_id) == row.exchange_order_id
    )


# ---------------------------------------------------------------------------
# (c): proving how a gone row ended
# ---------------------------------------------------------------------------


class _OrderGoneError(Exception):
    """The exchange definitively does not know this order id (-2013: evidence, not an error)."""


async def _terminalize(
    store: LiveOrderStore, client: Any, row: LiveOrderRecord, *, pair_venue: bool = False
) -> str | None:
    """Prove how a row the exchange no longer lists ended, and apply it monotonically.

    Positive evidence only: a fetch_order answer, or (for an OCO) every leg answering.
    Bybit pairs resolve by their two leg ids — a list id does not exist there — through
    the same leg-read primitive the poller uses. A refused or unchanged apply is a
    benign no-op (R16), not a finding; a lookup the exchange cannot answer is reported
    as drift and the row is left exactly as it was — never guessed terminal on negative
    evidence (F8's rule, applied to acked rows too).
    """
    if row.exchange_order_id is None:
        return None  # never acked: A1's resolution path owns this row
    try:
        if row.order_list_id is not None or row.order_type == OrderType.OCO.value:
            if pair_venue:
                return await _apply_proven(store, row, await _bybit_pair_answers(client, row))
            return await _apply_proven(store, row, await _oco_answers(client, row))
        answer = await _fetch_order(client, str(row.exchange_order_id), row.symbol)
        return await _apply_proven(store, row, [answer])
    except _OrderGoneError:
        return f"order {row.client_order_id} open locally, not found on the exchange"


async def _oco_answers(client: Any, row: LiveOrderRecord) -> list[dict[str, Any]]:
    """Every leg of an OCO row must answer (F9: list-aware, never fetch_order on the list id)."""
    if not row.leg_client_order_ids:
        raise _OrderGoneError
    return [
        await _fetch_order(client, leg_cid, row.symbol, by_client_id=True)
        for leg_cid in row.leg_client_order_ids
    ]


async def _bybit_pair_answers(client: Any, row: LiveOrderRecord) -> list[dict[str, Any]]:
    """The bybit pair's landed legs answer by their own client id (the tpsl namespace
    first, then the regular one) — never a list id, which bybit does not have. A leg cid
    that answers not-found is a rung that never landed (the degraded ladder's unused
    cids), not a gone order: only a pair where NOTHING answers is gone."""
    import ccxt

    answers: list[dict[str, Any]] = []
    for leg_cid in row.leg_client_order_ids:
        try:
            answers.append(await fetch_bybit_leg(client, leg_cid, row.symbol))
        except ccxt.OrderNotFound:
            continue
    if not answers:
        raise _OrderGoneError
    return answers


async def _fetch_order(
    client: Any, order_id: str, symbol: str, *, by_client_id: bool = False
) -> dict[str, Any]:
    """One order read; -2013 is evidence the order is gone, anything else is a read error."""
    import ccxt

    params = {"clientOrderId": order_id} if by_client_id else None
    try:
        return await client.fetch_order(order_id, symbol, params)
    except ccxt.OrderNotFound:
        raise _OrderGoneError from None
    except Exception as exc:
        raise _OrderReadError(str(exc)) from exc


async def _apply_proven(
    store: LiveOrderStore, row: LiveOrderRecord, answers: list[dict[str, Any]]
) -> str | None:
    """Apply the proven terminal status of a gone row; the finding names what was applied."""
    statuses = [map_ccxt_status(a.get("status"), a.get("filled")) for a in answers]
    fired = next(
        (a for a, s in zip(answers, statuses, strict=True) if s == LiveOrderStatus.FILLED),
        None,
    )
    if fired is not None:
        applied: LiveOrderStatus = LiveOrderStatus.FILLED
    elif len(answers) == 1 and statuses[0] in TERMINAL_STATUSES:
        applied = statuses[0]
        fired = answers[0]
    elif statuses and all(s in TERMINAL_STATUSES for s in statuses):
        applied = LiveOrderStatus.CANCELED  # an OCO that ended with no leg filled
        fired = answers[0]
    else:
        return None  # still open or unproven: nothing is applied, nothing is claimed
    outcome = await store.apply_status(
        row.client_order_id,
        applied,
        filled_quantity=_opt_d(fired.get("filled")),
        average_fill_price=_opt_d(fired.get("average")),
    )
    if outcome != "applied":
        return None  # R16: a refused or unchanged apply is a benign interleaving
    return f"order {row.client_order_id} {applied.value} on the exchange (status applied)"


# ---------------------------------------------------------------------------
# (f): the balance drift
# ---------------------------------------------------------------------------


def _balance_findings(
    local: dict[str, Decimal],
    wallet: dict[str, Decimal],
) -> list[str]:
    """The sleeve's own claim per asset against the wallet total (free + locked)."""
    findings: list[str] = []
    for asset in sorted(local):
        have = local[asset]
        if have <= 0:
            continue
        there = wallet.get(asset, Decimal("0"))
        if have - there > BALANCE_DRIFT_THRESHOLD * have:
            findings.append(f"balance drift {asset} local {_fmt(have)} exchange {_fmt(there)}")
    return findings


def _fmt(value: Decimal) -> str:
    """A Decimal exact but readable: no ledger exponent noise ('9000', not '9000.00000')."""
    return format(value.normalize(), "f")
