"""E2E: account drawdown -> breaker tiers -> tier-3 flatten -> audited reset -> trading resumes.

Real PaperTradingExecutor, OrderManager and RiskService; the flatten callback stands in for the
session owner's flatten primitive and submits reduce_only orders through the OrderManager.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hydra.core.types import Side
from hydra.execution.order_manager import RiskRejectedError
from hydra.risk.circuit_breakers import TierNotActiveError
from tests.unit.test_risk.harness import BTC, Account, make_account, order, permissive


async def _account_with_flatten() -> Account:
    holder: dict[str, Account] = {}

    async def flatten(reason: str) -> list[str]:
        acct = holder["acct"]
        failures: list[str] = []
        for sid, ex in acct.executors.items():
            for pos in await ex.get_positions():
                try:
                    await acct.managers[sid].submit_order(
                        order(Side.SELL, str(pos.quantity), reduce_only=True)
                    )
                except Exception as exc:
                    failures.append(f"{sid}: {exc}")
        return failures

    acct = await make_account(
        permissive(max_daily_loss_pct=Decimal("0.5")), on_must_flatten=flatten
    )
    holder["acct"] = acct
    await acct.add_session("s1", "10000")
    await acct.mark("60000")
    assert await acct.submit("s1", Side.BUY, "0.1")
    return acct


@pytest.mark.e2e
class TestCircuitBreakerFlow:
    async def test_tier3_flattens_then_audited_reset_resumes_trading(self) -> None:
        acct = await _account_with_flatten()
        ex = acct.executors["s1"]

        await acct.mark("57000")
        assert acct.svc.account().tier == 1
        assert await acct.svc.size_entry("s1", BTC, Decimal("0.01")) == Decimal("0.00085")

        await acct.mark("54800")
        assert acct.svc.account().tier == 2
        with pytest.raises(RiskRejectedError) as info:
            await acct.submit("s1", Side.BUY, "0.001")
        assert info.value.check == "circuit_breaker"

        await acct.mark("49500")
        assert acct.svc.account().tier == 3
        assert await ex.get_positions(BTC) == []
        assert (await ex.get_balance())["USDT"] == Decimal("8950.0")
        assert acct.buses["s1"].fills[-1].realized_pnl == Decimal("-1050.0")

        with pytest.raises(TierNotActiveError):
            await acct.svc.reset_breaker(2, actor="ops", reason="wrong tier", confirm_tier=2)
        await acct.svc.reset_breaker(3, actor="ops", reason="reviewed flatten", confirm_tier=3)
        kinds = [e.kind for e in acct.store.events]
        assert kinds.count("cb_escalate") == 3
        assert kinds[-1] == "cb_reset"

        await acct.mark("49600")
        assert acct.svc.account().tier == 0
        assert acct.svc.account().peak_equity == Decimal("8950.0")
        assert await acct.submit("s1", Side.BUY, "0.001")

    async def test_tier4_latches_kill_switch_and_reset_needs_confirmation(self) -> None:
        acct = await _account_with_flatten()
        await acct.mark("44900")
        assert acct.svc.account().tier == 4
        assert acct.svc.kill_switch_active() is True
        assert await acct.executors["s1"].get_positions(BTC) == []

        with pytest.raises(ValueError, match="confirm_tier"):
            await acct.svc.reset_breaker(4, actor="ops", reason="reviewed", confirm_tier=3)
        await acct.svc.reset_breaker(4, actor="ops", reason="reviewed", confirm_tier=4)
        assert acct.svc.account().tier == 0
        with pytest.raises(RiskRejectedError) as info:
            await acct.submit("s1", Side.BUY, "0.001")
        assert info.value.check == "kill_switch"

        await acct.svc.release_kill_switch("ops", "post-mortem done")
        assert await acct.submit("s1", Side.BUY, "0.001")
