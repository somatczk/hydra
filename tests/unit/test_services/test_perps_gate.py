"""The perps arm gate service: the separate ceremony, its state rows and the fail-closed
futures-permission re-fetch.

The DB boundary is an in-memory fake answering exactly the statements
``hydra.services.perps_gate`` issues; the exchange boundary is a fake ccxt ``binance``
class behind the real ``build_readonly_client``/``ExchangeClient``. Everything between
them is the real code, including Fernet decryption of the stored credentials and the
strategy-YAML venue read the live-start gates dispatch on.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import ccxt
import ccxt.async_support
import pytest
import yaml
from cryptography.fernet import Fernet

from hydra.core.encryption import encrypt
from hydra.core.types import MarketType
from hydra.services.approvals import ApprovalStore
from hydra.services.errors import ServiceError
from hydra.services.live_gate import (
    execute_live_order,
    execute_live_session_start,
    live_approval_store,
    read_live_state,
)
from hydra.services.perps_gate import (
    PERPS_ACTIONS,
    execute_perps_arming,
    is_perp_strategy,
    is_perp_venue,
    normalize_futures_permissions,
    positively_perps_disarmed,
    read_perps_capital,
    read_perps_state,
    require_futures_capable_credentials,
    require_perps_gate,
    running_live_perps_sessions,
    write_perps_armed_transition,
)
from hydra.strategy.config import load_strategy_config

ARMED_ROW = json.dumps({"armed": True, "by": "user:soma", "at": "2026-10-29T00:00:00+00:00"})

PERP_CONFIG: dict[str, Any] = {
    "id": "perp-momentum",
    "name": "perp-momentum",
    "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
    "symbols": ["BTCUSDT"],
    "exchange": {"exchange_id": "binanceusdm", "market_type": "FUTURES", "leverage": 2},
}
SPOT_CONFIG: dict[str, Any] = {
    "id": "spot-momentum",
    "name": "spot-momentum",
    "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
    "symbols": ["BTCUSDT"],
    "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
}
HAND_PERP_CONFIG: dict[str, Any] = {
    "id": "hand-perp",
    "name": "hand-perp",
    "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
    "symbols": ["BTCUSDT"],
    "exchange": {"exchange_id": "binanceusdm", "market_type": "FUTURES", "leverage": 2},
}
HAND_SPOT_CONFIG: dict[str, Any] = {
    "id": "hand-spot",
    "name": "hand-spot",
    "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
    "symbols": ["BTCUSDT"],
    "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
}


# ---------------------------------------------------------------------------
# Fakes: the DB boundary
# ---------------------------------------------------------------------------


class _Db:
    """Answers exactly the statements perps_gate issues; records every write."""

    def __init__(
        self,
        *,
        config: dict[str, str] | None = None,
        credentials: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.config: dict[str, str] = dict(config or {})
        self.credentials = dict(credentials or {})
        self.events: list[dict[str, Any]] = []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def value(self, key: str) -> Any:
        return json.loads(self.config[key])

    # -- pool -----------------------------------------------------------------

    def acquire(self) -> _Db:
        return self

    async def __aenter__(self) -> _Db:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        saved_config = dict(self.config)
        saved_events = list(self.events)
        try:
            yield
        except BaseException:
            self.config = saved_config
            self.events = saved_events
            raise

    # -- connection -----------------------------------------------------------

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.statements.append((sql, args))
        if sql == "SELECT value FROM system_config WHERE key = $1":
            value = self.config.get(args[0])
            return None if value is None else {"value": value}
        if sql.startswith("SELECT encrypted_key"):
            row = self.credentials.get(args[0])
            return None if row is None else dict(row)
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.statements.append((sql, args))
        if sql == "SELECT value FROM system_config WHERE key = $1":
            return self.config.get(args[0])
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        self.statements.append((sql, args))
        if sql.startswith("INSERT INTO system_config"):
            key, value, actor = args
            json.loads(value)
            self.config[key] = str(value)
            return "INSERT 0 1"
        if sql.startswith("INSERT INTO risk_events"):
            kind, actor, reason, details = args
            self.events.append(
                {
                    "scope": "paper",
                    "kind": kind,
                    "actor": actor,
                    "reason": reason,
                    "details": json.loads(details),
                }
            )
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {sql}")


class _BrokenPool:
    def acquire(self) -> _BrokenPool:
        return self

    async def __aenter__(self) -> _BrokenPool:
        raise OSError("db gone")

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Backend:
    """Records every call; the executor checks the kill switch through it."""

    def __init__(
        self,
        *,
        kill_switch: bool = False,
        sessions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.kill_switch = kill_switch
        self.sessions = sessions or []
        self.start_calls: list[dict[str, Any]] = []
        self.order_calls: list[Any] = []

    async def kill_switch_active(self) -> bool:
        return self.kill_switch

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self.sessions]

    async def start_session(self, actor: str, body: Any) -> dict[str, Any]:
        self.start_calls.append({"actor": actor, "body": body})
        return {"session_id": "s-live", "trading_mode": body.trading_mode, "created_by": actor}

    async def quick_trade(self, order: Any) -> dict[str, Any]:
        self.order_calls.append(order)
        return {"order_id": "o1", "session_id": order.session_id}


class _Deps:
    """Stands in for ``app.state``: the pool and the backend."""

    def __init__(self, db: _Db, backend: _Backend) -> None:
        self.db_pool = db
        self.trader = backend


# ---------------------------------------------------------------------------
# Fakes: the exchange boundary (behind the real build_readonly_client)
# ---------------------------------------------------------------------------


FUTURES_RESTRICTIONS = {
    "ipRestrict": False,
    "enableWithdrawals": False,
    "enableSpotAndMarginTrading": True,
    "enableFutures": True,
}


class FakeBinance:
    error: ClassVar[BaseException | None] = None
    restrictions: ClassVar[dict[str, Any]] = dict(FUTURES_RESTRICTIONS)
    instances: ClassVar[list[FakeBinance]] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.closed = False
        self.restriction_calls = 0
        type(self).instances.append(self)

    async def close(self) -> None:
        self.closed = True


async def _fake_get_account_api_restrictions(fake: FakeBinance, *args: Any) -> dict[str, Any]:
    fake.restriction_calls += 1
    cls = type(fake)
    if cls.error is not None:
        raise cls.error
    return dict(cls.restrictions)


# ccxt's camelCase implicit API name, bound as an attribute (pep8-naming flags the def)
FakeBinance.sapiGetAccountApiRestrictions = _fake_get_account_api_restrictions


class FakeBinanceUsdm(FakeBinance):
    """The USD⌂-M class stand-in: its own error/restrictions/instances, so a test can
    answer differently per venue (the arm reads the binanceusdm row first)."""

    error: ClassVar[BaseException | None] = None
    restrictions: ClassVar[dict[str, Any]] = dict(FUTURES_RESTRICTIONS)
    instances: ClassVar[list[FakeBinanceUsdm]] = []


@pytest.fixture()
def binance(monkeypatch: pytest.MonkeyPatch) -> type[FakeBinance]:
    FakeBinance.error = None
    FakeBinance.restrictions = dict(FUTURES_RESTRICTIONS)
    FakeBinance.instances = []
    monkeypatch.setattr(ccxt.async_support, "binance", FakeBinance)
    return FakeBinance


@pytest.fixture()
def binanceusdm(monkeypatch: pytest.MonkeyPatch) -> type[FakeBinanceUsdm]:
    FakeBinanceUsdm.error = None
    FakeBinanceUsdm.restrictions = dict(FUTURES_RESTRICTIONS)
    FakeBinanceUsdm.instances = []
    monkeypatch.setattr(ccxt.async_support, "binanceusdm", FakeBinanceUsdm)
    return FakeBinanceUsdm


@pytest.fixture(autouse=True)
def credential_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def perp_strategies(strategies_dir: Any) -> Any:
    """One perps strategy and one spot strategy on disk: the venue truth the live-start
    gates dispatch on."""
    for cfg in (PERP_CONFIG, SPOT_CONFIG):
        path = strategies_dir / f"{cfg['id']}.yaml"
        path.write_text(yaml.safe_dump(cfg))
    return strategies_dir


def _creds_row(
    *,
    environment: str = "mainnet",
    key: str = "key-1",
) -> dict[str, Any]:
    return {
        "encrypted_key": encrypt(key),
        "encrypted_secret": encrypt("secret-1"),
        "encrypted_passphrase": None,
        "validated_environment": environment,
    }


def _detail(exc: ServiceError) -> dict[str, Any]:
    assert isinstance(exc.detail, dict)
    return exc.detail


async def _perps_arm(db: _Db, backend: _Backend, **params: Any) -> dict[str, Any]:
    body = {"perps_capital": 500.0, "reason": "arm perps"}
    body.update(params)
    return await execute_perps_arming(_Deps(db, backend), body, "user:soma")


# ---------------------------------------------------------------------------
# The armed rows
# ---------------------------------------------------------------------------


class TestPerpsState:
    async def test_absent_or_broken_rows_read_disarmed(self) -> None:
        assert await read_perps_state(None) == {
            "armed": False,
            "armed_by": None,
            "armed_at": None,
        }
        assert (await read_perps_state(_Db()))["armed"] is False
        assert (await read_perps_state(_BrokenPool()))["armed"] is False

    async def test_the_armed_row_reads_back_with_its_attribution(self) -> None:
        db = _Db(config={"perps_armed": ARMED_ROW})
        assert await read_perps_state(db) == {
            "armed": True,
            "armed_by": "user:soma",
            "armed_at": "2026-10-29T00:00:00+00:00",
        }

    async def test_the_spot_armed_row_is_not_the_perps_row(self) -> None:
        """The ceremonies are separate: a spot-armed platform does not arm perps."""
        db = _Db(config={"live_armed": ARMED_ROW, "live_capital": "500.0"})
        assert (await read_perps_state(db))["armed"] is False
        db = _Db(config={"perps_armed": ARMED_ROW, "perps_capital": "500.0"})
        assert (await read_live_state(db))["armed"] is False

    async def test_perps_capital_read(self) -> None:
        db = _Db(config={"perps_capital": "500.0"})
        assert await read_perps_capital(db) == 500.0
        assert await read_perps_capital(_Db()) is None
        assert await read_perps_capital(None) is None
        assert await read_perps_capital(_Db(config={"perps_capital": '"-1"'})) is None

    async def test_positively_perps_disarmed_needs_a_read_that_answers(self) -> None:
        assert await positively_perps_disarmed(_Db()) is True  # no row at all
        db = _Db(config={"perps_armed": json.dumps({"armed": False, "by": "u", "at": "x"})})
        assert await positively_perps_disarmed(db) is True
        with pytest.raises(ServiceError) as exc:
            await positively_perps_disarmed(_BrokenPool())
        assert _detail(exc.value)["code"] == "database_unavailable"

    async def test_write_perps_armed_transition_writes_rows_and_event_together(self) -> None:
        db = _Db()
        written = await write_perps_armed_transition(
            db, armed=True, actor="user:soma", reason="arm perps", perps_capital=500.0
        )
        assert written["armed"] is True
        assert db.value("perps_armed") == {
            "armed": True,
            "by": "user:soma",
            "at": written["at"],
        }
        assert db.value("perps_capital") == 500.0
        assert db.events == [
            {
                "scope": "paper",
                "kind": "perps_armed",
                "actor": "user:soma",
                "reason": "arm perps",
                "details": {"perps_capital": 500.0},
            }
        ]

    async def test_disarm_writes_the_row_and_a_bare_event(self) -> None:
        db = _Db(config={"perps_armed": ARMED_ROW})
        await write_perps_armed_transition(db, armed=False, actor="user:soma", reason="off")
        assert db.value("perps_armed")["armed"] is False
        assert db.events == [
            {
                "scope": "paper",
                "kind": "perps_disarmed",
                "actor": "user:soma",
                "reason": "off",
                "details": {},
            }
        ]

    async def test_a_broken_write_raises_and_writes_nothing(self) -> None:
        with pytest.raises(ServiceError) as exc:
            await write_perps_armed_transition(_BrokenPool(), armed=True, actor="a", reason="r")
        assert _detail(exc.value)["code"] == "database_unavailable"


# ---------------------------------------------------------------------------
# require_perps_gate: the positive precondition every live FUTURES surface checks
# ---------------------------------------------------------------------------


class TestRequirePerpsGate:
    async def test_unarmed_refuses_with_409_perps_not_armed(self) -> None:
        with pytest.raises(ServiceError) as exc:
            await require_perps_gate(_Db())
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("perps_not_armed", 409)
        assert "POST /api/live/perps/arm" in _detail(exc.value)["message"]

    async def test_pool_none_counts_unarmed_never_503(self) -> None:
        with pytest.raises(ServiceError) as exc:
            await require_perps_gate(None)
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("perps_not_armed", 409)

    async def test_armed_without_capital_is_403_perps_capital_missing(self) -> None:
        db = _Db(config={"perps_armed": ARMED_ROW})
        with pytest.raises(ServiceError) as exc:
            await require_perps_gate(db)
        assert (_detail(exc.value)["code"], exc.value.status_code) == (
            "perps_capital_missing",
            403,
        )
        assert "disarm" in _detail(exc.value)["message"]  # re-arming alone is a no-op

    async def test_armed_with_capital_returns_the_state(self) -> None:
        db = _Db(config={"perps_armed": ARMED_ROW, "perps_capital": "500.0"})
        assert await require_perps_gate(db) == {
            "armed": True,
            "armed_by": "user:soma",
            "armed_at": "2026-10-29T00:00:00+00:00",
        }

    async def test_the_spot_rows_do_not_satisfy_the_perps_gate(self) -> None:
        db = _Db(config={"live_armed": ARMED_ROW, "live_capital": "500.0"})
        with pytest.raises(ServiceError) as exc:
            await require_perps_gate(db)
        assert _detail(exc.value)["code"] == "perps_not_armed"


# ---------------------------------------------------------------------------
# The futures-permission re-fetch fail-closed (the heart)
# ---------------------------------------------------------------------------


class TestRequireFuturesCapableCredentials:
    async def test_no_keys_on_either_venue_is_403_naming_both(
        self, binance: type[FakeBinance], binanceusdm: type[FakeBinanceUsdm]
    ) -> None:
        with pytest.raises(ServiceError) as exc:
            await require_futures_capable_credentials(_Db())
        assert (_detail(exc.value)["code"], exc.value.status_code) == (
            "live_credentials_missing",
            403,
        )
        assert _detail(exc.value)["message"] == (
            "No binanceusdm or binance credentials are stored; connect keys in Settings first"
        )
        assert FakeBinance.instances == [] and FakeBinanceUsdm.instances == []

    async def test_undecryptable_keys_are_403(self, binance: type[FakeBinance]) -> None:
        db = _Db(credentials={"binance": {**_creds_row(), "encrypted_key": "not-fernet"}})
        with pytest.raises(ServiceError) as exc:
            await require_futures_capable_credentials(db)
        assert _detail(exc.value)["code"] == "live_credentials_missing"
        assert FakeBinance.instances == []

    async def test_keys_validated_for_neither_environment_are_refused(
        self, binance: type[FakeBinance]
    ) -> None:
        for environment in ("", None):
            row = _creds_row(environment="mainnet")
            row["validated_environment"] = environment
            with pytest.raises(ServiceError) as exc:
                await require_futures_capable_credentials(_Db(credentials={"binance": row}))
            assert (_detail(exc.value)["code"], exc.value.status_code) == (
                "credentials_not_futures_capable",
                409,
            )
        assert FakeBinance.instances == []

    async def test_futures_enabled_trade_only_keys_pass_via_the_live_re_fetch(
        self, binance: type[FakeBinance]
    ) -> None:
        db = _Db(credentials={"binance": _creds_row()})
        assert await require_futures_capable_credentials(db) == {
            "enable_futures": True,
            "enable_withdrawals": False,
        }
        [probe] = FakeBinance.instances
        assert probe.restriction_calls == 1  # the arm-time re-fetch, never the snapshot
        assert probe.opts["apiKey"] == "key-1"
        assert "sandbox" not in probe.opts  # the mainnet client
        assert probe.closed is True

    async def test_no_usdm_row_falls_back_to_the_spot_row_and_its_client(
        self, binance: type[FakeBinance], binanceusdm: type[FakeBinanceUsdm]
    ) -> None:
        """A2 (the #220 review, a pre-existing P4-E root): the single-key operator's
        working path — only a spot row stored means the arm still checks that row, with
        the spot client, exactly as before."""
        db = _Db(credentials={"binance": _creds_row()})
        assert (await require_futures_capable_credentials(db))["enable_futures"] is True
        [probe] = FakeBinance.instances
        assert probe.opts["apiKey"] == "key-1"
        assert FakeBinanceUsdm.instances == []

    async def test_the_arm_prefers_the_usdm_row_over_a_read_only_spot_row(
        self, binance: type[FakeBinance], binanceusdm: type[FakeBinanceUsdm]
    ) -> None:
        """The reviewer's dead end: a futures-capable USD⌂-M row beside a read-only
        spot row used to end in a fail-closed 409 naming the spot key — the arm now
        proceeds on the perps venue's key, the row a live perp session trades with."""
        db = _Db(
            credentials={
                "binanceusdm": _creds_row(key="key-usdm"),
                "binance": _creds_row(key="key-spot"),
            }
        )
        FakeBinanceUsdm.restrictions = dict(FUTURES_RESTRICTIONS)
        FakeBinance.restrictions = {
            "ipRestrict": False,
            "enableWithdrawals": False,
            "enableSpotAndMarginTrading": True,
        }  # the spot key: futures permission absent, read-only
        assert await require_futures_capable_credentials(db) == {
            "enable_futures": True,
            "enable_withdrawals": False,
        }
        [probe] = FakeBinanceUsdm.instances
        assert probe.restriction_calls == 1
        assert probe.opts["apiKey"] == "key-usdm"
        assert probe.closed is True
        assert FakeBinance.instances == []  # the spot row was never read

    async def test_every_refusal_names_the_venue_of_the_key_it_refused(
        self, binance: type[FakeBinance], binanceusdm: type[FakeBinanceUsdm]
    ) -> None:
        FakeBinanceUsdm.restrictions = {**FUTURES_RESTRICTIONS, "enableFutures": False}
        db = _Db(
            credentials={"binanceusdm": _creds_row(), "binance": _creds_row()},
        )
        with pytest.raises(ServiceError) as exc:
            await require_futures_capable_credentials(db)
        assert _detail(exc.value)["message"] == (
            "The stored binanceusdm key cannot trade futures; perps trading needs "
            "futures enabled on the key (enable futures trading on the key and "
            "reconnect it in Settings)"
        )
        FakeBinance.restrictions = {**FUTURES_RESTRICTIONS, "enableWithdrawals": True}
        db = _Db(credentials={"binance": _creds_row()})
        with pytest.raises(ServiceError) as exc:
            await require_futures_capable_credentials(db)
        assert _detail(exc.value)["message"] == (
            "The stored binance key has withdrawals enabled; perps trading needs "
            "a trade-only key (disable withdrawals on the key and reconnect it "
            "in Settings)"
        )

    async def test_an_absent_enable_futures_flag_refuses_arming(
        self, binance: type[FakeBinance]
    ) -> None:
        FakeBinance.restrictions = {
            "ipRestrict": False,
            "enableWithdrawals": False,
            "enableSpotAndMarginTrading": True,
        }
        with pytest.raises(ServiceError) as exc:
            await require_futures_capable_credentials(_Db(credentials={"binance": _creds_row()}))
        assert (_detail(exc.value)["code"], exc.value.status_code) == (
            "credentials_not_futures_capable",
            409,
        )
        assert "cannot trade futures" in _detail(exc.value)["message"]

    async def test_a_false_enable_futures_flag_refuses_arming(
        self, binance: type[FakeBinance]
    ) -> None:
        FakeBinance.restrictions = {**FUTURES_RESTRICTIONS, "enableFutures": False}
        with pytest.raises(ServiceError) as exc:
            await require_futures_capable_credentials(_Db(credentials={"binance": _creds_row()}))
        assert _detail(exc.value)["code"] == "credentials_not_futures_capable"

    async def test_withdrawals_enabled_refuses_arming(self, binance: type[FakeBinance]) -> None:
        FakeBinance.restrictions = {**FUTURES_RESTRICTIONS, "enableWithdrawals": True}
        with pytest.raises(ServiceError) as exc:
            await require_futures_capable_credentials(_Db(credentials={"binance": _creds_row()}))
        assert _detail(exc.value)["code"] == "credentials_not_futures_capable"
        assert "withdrawals" in _detail(exc.value)["message"]

    @pytest.mark.parametrize(
        "error", [ccxt.NetworkError("dns"), ccxt.AuthenticationError("revoked"), TimeoutError()]
    )
    async def test_an_unreadable_re_fetch_refuses_arming(
        self, binance: type[FakeBinance], error: BaseException
    ) -> None:
        FakeBinance.error = error
        with pytest.raises(ServiceError) as exc:
            await require_futures_capable_credentials(_Db(credentials={"binance": _creds_row()}))
        assert _detail(exc.value)["code"] == "credentials_not_futures_capable"
        assert "nothing was armed" in _detail(exc.value)["message"]

    async def test_testnet_keys_re_fetch_through_the_sandbox_client_and_can_arm(
        self, binance: type[FakeBinance]
    ) -> None:
        """The battery arms the perps ceremony on testnet: the same read, the sandbox
        client, the same fail-closed truth — a permission the testnet cannot confirm
        refuses instead of being skipped."""
        db = _Db(credentials={"binance": _creds_row(environment="testnet")})
        assert (await require_futures_capable_credentials(db))["enable_futures"] is True
        [probe] = FakeBinance.instances
        assert probe.opts["sandbox"] is True
        FakeBinance.restrictions = {
            k: v for k, v in FUTURES_RESTRICTIONS.items() if k != "enableFutures"
        }
        FakeBinance.instances = []
        db = _Db(credentials={"binance": _creds_row(environment="testnet")})
        with pytest.raises(ServiceError) as exc:
            await require_futures_capable_credentials(db)
        assert (_detail(exc.value)["code"], exc.value.status_code) == (
            "credentials_not_futures_capable",
            409,
        )


class TestNormalizeFuturesPermissions:
    def test_maps_the_binance_flags(self) -> None:
        assert normalize_futures_permissions(FUTURES_RESTRICTIONS) == {
            "enable_futures": True,
            "enable_withdrawals": False,
        }
        assert normalize_futures_permissions({}) == {
            "enable_futures": False,
            "enable_withdrawals": False,
        }
        assert normalize_futures_permissions(None)["enable_futures"] is False


# ---------------------------------------------------------------------------
# The live_perps_arming executor
# ---------------------------------------------------------------------------


class TestPerpsArmingExecutor:
    async def test_arms_and_writes_the_capital_as_one_transition(
        self, binance: type[FakeBinance]
    ) -> None:
        db = _Db(credentials={"binance": _creds_row()})
        result = await _perps_arm(db, _Backend())
        assert result["status"] == "armed"
        assert result["perps_capital"] == 500.0
        assert result["armed_by"] == "user:soma"
        assert db.value("perps_armed") == {
            "armed": True,
            "by": "user:soma",
            "at": result["armed_at"],
        }
        assert db.value("perps_capital") == 500.0
        assert db.events == [
            {
                "scope": "paper",
                "kind": "perps_armed",
                "actor": "user:soma",
                "reason": "arm perps",
                "details": {"perps_capital": 500.0},
            }
        ]
        [probe] = FakeBinance.instances
        assert probe.restriction_calls == 1  # the arm re-fetched the permission live

    async def test_the_kill_switch_refuses_arming_before_any_read(
        self, binance: type[FakeBinance]
    ) -> None:
        db = _Db(credentials={"binance": _creds_row()})
        with pytest.raises(ServiceError) as exc:
            await _perps_arm(db, _Backend(kill_switch=True))
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("kill_switch_active", 409)
        assert "perps_armed" not in db.config
        assert FakeBinance.instances == []

    async def test_keys_without_futures_permission_refuse_arming_before_any_write(
        self, binance: type[FakeBinance]
    ) -> None:
        FakeBinance.restrictions = {**FUTURES_RESTRICTIONS, "enableFutures": False}
        db = _Db(credentials={"binance": _creds_row()})
        with pytest.raises(ServiceError) as exc:
            await _perps_arm(db, _Backend())
        assert _detail(exc.value)["code"] == "credentials_not_futures_capable"
        assert "perps_armed" not in db.config
        assert "perps_capital" not in db.config
        assert db.events == []

    async def test_a_bad_perps_capital_is_refused(self, binance: type[FakeBinance]) -> None:
        db = _Db(credentials={"binance": _creds_row()})
        for bad in (None, 0, -1, "nan"):
            with pytest.raises(ServiceError) as exc:
                await _perps_arm(db, _Backend(), perps_capital=bad)
            assert _detail(exc.value)["code"] == "invalid_params"
        assert "perps_armed" not in db.config

    async def test_already_armed_is_a_clean_no_op(self, binance: type[FakeBinance]) -> None:
        db = _Db(
            config={"perps_armed": ARMED_ROW, "perps_capital": "250.0"},
            credentials={"binance": _creds_row()},
        )
        result = await _perps_arm(db, _Backend(), perps_capital=999.0)
        assert result["status"] == "already_armed"
        assert result["perps_capital"] == 250.0  # the capital is not silently changed
        assert db.value("perps_capital") == 250.0
        assert db.events == []


# ---------------------------------------------------------------------------
# The store registration and the venue truth
# ---------------------------------------------------------------------------


def test_the_perps_action_registers_on_the_shared_store() -> None:
    deps = _Deps(_Db(), _Backend())
    store = live_approval_store(deps)
    assert isinstance(store, ApprovalStore)
    assert live_approval_store(deps) is store  # idempotent, one store
    assert PERPS_ACTIONS == ("live_perps_arming",)


class TestVenueTruth:
    def test_is_perp_strategy_reads_the_saved_yaml(self) -> None:
        assert is_perp_strategy("perp-momentum") is True
        assert is_perp_strategy("spot-momentum") is False
        assert is_perp_strategy("missing") is False

    def test_a_hand_written_yml_strategy_reads_the_same_venue_truth(
        self, strategies_dir: Any
    ) -> None:
        """The A1 corner: a hand-written ``.yml`` config is invisible to the shared
        ``.yaml``-only scan the gate used to borrow, yet the manager's loader accepts
        it — the gate must read the venue the loader reads."""
        (strategies_dir / "hand-perp.yml").write_text(yaml.safe_dump(HAND_PERP_CONFIG))
        (strategies_dir / "hand-spot.yml").write_text(yaml.safe_dump(HAND_SPOT_CONFIG))
        assert is_perp_strategy("hand-perp") is True  # was False while the loader saw FUTURES
        assert is_perp_strategy("hand-spot") is False
        loaded = load_strategy_config(strategies_dir / "hand-perp.yml")
        assert loaded.exchange.market_type is MarketType.FUTURES  # the manager's own truth

    def test_is_perp_venue(self) -> None:
        assert is_perp_venue("binanceusdm") is True
        assert is_perp_venue("binance") is False
        assert is_perp_venue(None) is False
        assert is_perp_venue("") is False

    async def test_running_live_perps_sessions_filter_by_venue(self) -> None:
        backend = _Backend(
            sessions=[
                {
                    "session_id": "s-perp",
                    "trading_mode": "live",
                    "status": "running",
                    "exchange_id": "binanceusdm",
                },
                {
                    "session_id": "s-spot",
                    "trading_mode": "live",
                    "status": "running",
                    "exchange_id": "binance",
                },
                {
                    "session_id": "s-perp-stopped",
                    "trading_mode": "live",
                    "status": "stopped",
                    "exchange_id": "binanceusdm",
                },
                {
                    "session_id": "s-perp-paper",
                    "trading_mode": "paper",
                    "status": "running",
                    "exchange_id": "binanceusdm",
                },
            ]
        )
        assert await running_live_perps_sessions(backend) == ["s-perp"]


# ---------------------------------------------------------------------------
# The gate's reach: the live executors check the perps gate on FUTURES surfaces
# ---------------------------------------------------------------------------


class TestTheGateReach:
    async def test_a_live_futures_start_is_refused_while_perps_is_unarmed(
        self, binance: type[FakeBinance]
    ) -> None:
        deps = _Deps(_Db(), _Backend())
        with pytest.raises(ServiceError) as exc:
            await execute_live_session_start(
                deps, {"strategy_id": "perp-momentum", "reason": "go live"}, "user:soma"
            )
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("perps_not_armed", 409)
        assert deps.trader.start_calls == []

    async def test_a_live_futures_start_is_refused_armed_without_capital(
        self, binance: type[FakeBinance]
    ) -> None:
        deps = _Deps(_Db(config={"perps_armed": ARMED_ROW}), _Backend())
        with pytest.raises(ServiceError) as exc:
            await execute_live_session_start(
                deps, {"strategy_id": "perp-momentum", "reason": "go live"}, "user:soma"
            )
        assert (_detail(exc.value)["code"], exc.value.status_code) == (
            "perps_capital_missing",
            403,
        )
        assert deps.trader.start_calls == []

    async def test_armed_perps_with_capital_passes_the_gate_to_the_backend(self) -> None:
        """The positive precondition P4-D's live executor will consume: the spot gate
        is NOT consulted for a perps start (the ceremonies are independent)."""
        deps = _Deps(_Db(config={"perps_armed": ARMED_ROW, "perps_capital": "500.0"}), _Backend())
        result = await execute_live_session_start(
            deps, {"strategy_id": "perp-momentum", "reason": "go live"}, "user:soma"
        )
        assert result["trading_mode"] == "live"
        [call] = deps.trader.start_calls
        assert call["body"].strategy_id == "perp-momentum"

    async def test_a_live_spot_start_keeps_the_spot_gate(self) -> None:
        deps = _Deps(_Db(), _Backend())
        with pytest.raises(ServiceError) as exc:
            await execute_live_session_start(
                deps, {"strategy_id": "spot-momentum", "reason": "go live"}, "user:soma"
            )
        assert _detail(exc.value)["code"] == "live_not_armed"

    async def test_a_yml_perp_strategy_answers_to_the_perps_ceremony_even_spot_armed(
        self, strategies_dir: Any
    ) -> None:
        """The A1 hole, pre-fix: a hand-written ``.yml`` FUTURES config dispatched to
        the spot gate, so a fully-armed spot ceremony authorized its live start."""
        (strategies_dir / "hand-perp.yml").write_text(yaml.safe_dump(HAND_PERP_CONFIG))
        deps = _Deps(_Db(config={"live_armed": ARMED_ROW, "live_capital": "500.0"}), _Backend())
        with pytest.raises(ServiceError) as exc:
            await execute_live_session_start(
                deps, {"strategy_id": "hand-perp", "reason": "go live"}, "user:soma"
            )
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("perps_not_armed", 409)
        assert deps.trader.start_calls == []

    async def test_a_yml_spot_strategy_keeps_the_spot_gate(self, strategies_dir: Any) -> None:
        (strategies_dir / "hand-spot.yml").write_text(yaml.safe_dump(HAND_SPOT_CONFIG))
        deps = _Deps(_Db(), _Backend())
        with pytest.raises(ServiceError) as exc:
            await execute_live_session_start(
                deps, {"strategy_id": "hand-spot", "reason": "go live"}, "user:soma"
            )
        assert _detail(exc.value)["code"] == "live_not_armed"

    async def test_a_manual_live_order_on_a_perps_session_checks_the_perps_gate(self) -> None:
        perp_session = {
            "session_id": "s-perp",
            "trading_mode": "live",
            "status": "running",
            "exchange_id": "binanceusdm",
        }
        deps = _Deps(_Db(), _Backend(sessions=[perp_session]))
        with pytest.raises(ServiceError) as exc:
            await execute_live_order(
                deps,
                {"session_id": "s-perp", "side": "buy", "quantity": "1", "reason": "r"},
                "user:soma",
            )
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("perps_not_armed", 409)
        assert deps.trader.order_calls == []

        deps = _Deps(
            _Db(config={"perps_armed": ARMED_ROW, "perps_capital": "500.0"}),
            _Backend(sessions=[perp_session]),
        )
        result = await execute_live_order(
            deps,
            {"session_id": "s-perp", "side": "buy", "quantity": "1", "reason": "r"},
            "user:soma",
        )
        assert result["order_id"] == "o1"

    async def test_a_manual_live_order_on_a_spot_session_keeps_the_spot_gate(self) -> None:
        spot_session = {
            "session_id": "s-spot",
            "trading_mode": "live",
            "status": "running",
            "exchange_id": "binance",
        }
        deps = _Deps(_Db(), _Backend(sessions=[spot_session]))
        with pytest.raises(ServiceError) as exc:
            await execute_live_order(
                deps,
                {"session_id": "s-spot", "side": "buy", "quantity": "1", "reason": "r"},
                "user:soma",
            )
        assert _detail(exc.value)["code"] == "live_not_armed"
