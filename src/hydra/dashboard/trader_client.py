"""RemoteTrader: the :class:`TraderBackend` over HTTP to the trader's internal API (D5).

The two-process half of the seam: every op is one bearer-authenticated call to
``hydra.trader.server`` (``HYDRA_TRADER__URL``, ``HYDRA_API_TOKEN``, ``trader.timeout_s``).
The wire format is the shared envelope — a non-2xx body rehydrates to the same
:class:`~hydra.services.errors.ServiceError` with its code, state and extras verbatim (a 409
keeps ``changed_fields``), and a transport error or timeout is seam #8's 503
``engine_unreachable``. Exactly the 19 pinned ops, like :class:`~hydra.services.trader.LocalTrader`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from hydra.core.config import HydraConfig
from hydra.execution.session_manager import HealthSnapshot, SessionHealth
from hydra.services.errors import ServiceError, error_detail, service_error
from hydra.services.sessions import QuickTradeRequest, StartSessionRequest

_UNREACHABLE = "The trading engine is unreachable (HYDRA_TRADER__URL)"
_TIMED_OUT = (
    "The trading engine did not answer within {timeout}s — the operation may still be "
    "executing; check the session or order state before re-submitting"
)


def _snapshot_from_body(body: dict[str, Any]) -> HealthSnapshot:
    """Rehydrate the serialized :meth:`health_snapshot` answer (server.py's mirror)."""
    sessions = []
    for raw in body["sessions"]:
        row = dict(raw)
        for field in ("last_bar_at", "last_fill_at", "last_reconciled_at"):
            row[field] = None if row[field] is None else datetime.fromisoformat(row[field])
        row["protection_failures"] = tuple(row["protection_failures"])
        sessions.append(SessionHealth(**row))
    return HealthSnapshot(
        sessions=tuple(sessions),
        desired_from_db=bool(body["desired_from_db"]),
        kill_switch=bool(body["kill_switch"]),
        risk_available=bool(body["risk_available"]),
    )


class RemoteTrader:
    """The trader behind ``HYDRA_TRADER__URL``; close with :meth:`aclose` at shutdown."""

    def __init__(self, cfg: HydraConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        url = cfg.trader.url
        if url is None:
            raise ValueError("RemoteTrader needs trader.url (HYDRA_TRADER__URL)")
        token = cfg.api_token.get_secret_value() if cfg.api_token is not None else None
        self._timeout_s = cfg.trader.timeout_s
        self._client = httpx.AsyncClient(
            base_url=url,
            timeout=cfg.trader.timeout_s,
            headers={"Authorization": f"Bearer {token}"} if token is not None else None,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> RemoteTrader:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
    ) -> Any:
        try:
            response = await self._client.request(method, path, json=json, params=params)
        except httpx.TimeoutException as exc:
            # a timeout is in-flight-unknown, not a not-applied — the op may be executing
            raise service_error(
                503, "engine_unreachable", _TIMED_OUT.format(timeout=self._timeout_s), "unavailable"
            ) from exc
        except httpx.RequestError as exc:
            raise service_error(503, "engine_unreachable", _UNREACHABLE, "unavailable") from exc
        if response.status_code >= 400:
            raise self._rehydrate(response)
        return None if not response.content else response.json()

    @staticmethod
    def _rehydrate(response: httpx.Response) -> ServiceError:
        """The envelope body back to a ServiceError, verbatim; anything else is honest 503s."""
        try:
            body = response.json()
        except ValueError:
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        if not isinstance(detail, dict):
            detail = error_detail(
                "engine_error",
                f"The trading engine answered HTTP {response.status_code}",
                "unavailable",
                status_code=response.status_code,
            )
        return ServiceError(response.status_code, detail)

    # -- Sessions -----------------------------------------------------------

    async def start_session(self, actor: str, body: StartSessionRequest) -> dict[str, Any]:
        result: dict[str, Any] = await self._call(
            "POST",
            "/ops/start-session",
            json={"actor": actor, "body": body.model_dump(mode="json")},
        )
        return result

    async def stop_session(self, session_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("DELETE", f"/ops/sessions/{session_id}")
        return result

    async def list_sessions(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._call("GET", "/ops/sessions")
        return result

    async def session_detail(self, session_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("GET", f"/ops/sessions/{session_id}/detail")
        return result

    async def quick_trade(self, order: QuickTradeRequest) -> dict[str, Any]:
        result: dict[str, Any] = await self._call(
            "POST", "/ops/quick-trade", json={"order": order.model_dump(mode="json")}
        )
        return result

    async def webhook_signal(
        self,
        *,
        symbol: str,
        side: str,
        action: str,
        quantity: Decimal | None,
        strategy_id: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = await self._call(
            "POST",
            "/ops/webhook-signal",
            json={
                "symbol": symbol,
                "side": side,
                "action": action,
                # the Decimal-as-string shape model_dump(mode="json") uses — never float()
                "quantity": None if quantity is None else str(quantity),
                "strategy_id": strategy_id,
            },
        )
        return result

    async def reload_strategy(self, strategy_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("POST", f"/ops/strategies/{strategy_id}/reload")
        return result

    # -- Kill switch ----------------------------------------------------------

    async def activate_kill_switch(self, actor: str, reason: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._call(
            "POST", "/ops/kill-switch", json={"actor": actor, "reason": reason}
        )
        return result

    async def release_kill_switch(self, actor: str, reason: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._call(
            "DELETE", "/ops/kill-switch", json={"actor": actor, "reason": reason}
        )
        return result

    async def kill_switch_active(self) -> bool:
        body = await self._call("GET", "/ops/kill-switch/active")
        return bool(body["active"])

    # -- Risk reads and mutations ----------------------------------------------

    async def risk_status(self) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("GET", "/ops/risk/status")
        return result

    async def var_estimate(self) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("GET", "/ops/risk/var")
        return result

    async def circuit_breakers(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._call("GET", "/ops/risk/breakers")
        return result

    async def reset_breaker(
        self, tier: int, actor: str, reason: str, confirm_tier: int
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._call(
            "POST",
            "/ops/risk/breakers/reset",
            json={"actor": actor, "reason": reason, "tier": tier, "confirm_tier": confirm_tier},
        )
        return result

    async def reset_paper_account(self, actor: str, reason: str) -> None:
        await self._call(
            "POST", "/ops/risk/paper-account/reset", json={"actor": actor, "reason": reason}
        )

    async def live_status(self, source: str = "paper") -> dict[str, Any]:
        result: dict[str, Any] = await self._call(
            "GET", "/ops/risk/live-status", params={"source": source}
        )
        return result

    async def positions(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._call("GET", "/ops/positions")
        return result

    async def reload_paper_capital(self, actor: str) -> None:
        await self._call("POST", "/ops/risk/paper-capital/reload", json={"actor": actor})

    async def health_snapshot(self) -> HealthSnapshot:
        return _snapshot_from_body(await self._call("GET", "/ops/health-snapshot"))
