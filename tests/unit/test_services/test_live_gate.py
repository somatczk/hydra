"""The live gate service: the armed row, trade-only credentials and the four executors.

The DB boundary is an in-memory fake answering exactly the statements
``hydra.services.live_gate`` issues; the exchange boundary is a fake ccxt ``binance``
class behind the real ``build_readonly_client``/``ExchangeClient``. Everything between
them is the real code, including Fernet decryption of the stored credentials.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, ClassVar

import ccxt
import ccxt.async_support
import pytest
from cryptography.fernet import Fernet

from hydra.core.encryption import encrypt
from hydra.services import live_gate
from hydra.services.approvals import ApprovalStore
from hydra.services.errors import ServiceError
from hydra.services.live_gate import (
    create_live_approval,
    execute_credential_delete,
    execute_live_arming,
    execute_live_order,
    execute_live_session_start,
    is_trade_capable,
    live_approval_store,
    normalize_restrictions,
    read_live_capital,
    read_live_state,
    require_live_capable_credentials,
    stored_permissions,
    write_armed_transition,
)
from hydra.services.sessions import StartSessionRequest

ARMED_ROW = json.dumps({"armed": True, "by": "user:soma", "at": "2026-10-28T00:00:00+00:00"})


# ---------------------------------------------------------------------------
# Fakes: the DB boundary
# ---------------------------------------------------------------------------


class _Db:
    """Answers exactly the statements live_gate issues; records every write."""

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
        saved_credentials = dict(self.credentials)
        saved_events = list(self.events)
        try:
            yield
        except BaseException:
            self.config = saved_config
            self.credentials = saved_credentials
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
        if sql == "SELECT 1 FROM exchange_credentials WHERE exchange_id = $1":
            return None if args[0] not in self.credentials else {"one": 1}
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
            if sql.startswith(
                "INSERT INTO risk_events (scope, kind, tier, actor, reason, "
                "details) VALUES ('paper'"
            ):
                kind, actor, reason, details = args
                scope, _tier = "paper", None
            else:
                scope, kind, _tier, actor, reason, details = args
            self.events.append(
                {
                    "scope": scope,
                    "kind": kind,
                    "actor": actor,
                    "reason": reason,
                    "details": json.loads(details),
                }
            )
            return "INSERT 0 1"
        if sql == "DELETE FROM exchange_credentials WHERE exchange_id = $1":
            self.credentials.pop(args[0], None)
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {sql}")


class _BrokenPool:
    def acquire(self) -> _BrokenPool:
        return self

    async def __aenter__(self) -> _BrokenPool:
        raise OSError("db gone")

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Backend:
    """Records every call; the executors go through it, never around it."""

    def __init__(
        self,
        *,
        kill_switch: bool = False,
        sessions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.kill_switch = kill_switch
        self.sessions = sessions or []
        self.start_calls: list[tuple[str, StartSessionRequest]] = []
        self.order_calls: list[Any] = []

    async def kill_switch_active(self) -> bool:
        return self.kill_switch

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self.sessions]

    async def start_session(self, actor: str, body: StartSessionRequest) -> dict[str, Any]:
        self.start_calls.append((actor, body))
        return {"session_id": "s-live", "trading_mode": body.trading_mode, "created_by": actor}

    async def quick_trade(self, order: Any) -> dict[str, Any]:
        self.order_calls.append(order)
        return {"order_id": "o1", "session_id": order.session_id}


class _Deps:
    """Stands in for ``app.state``: the pool, the backend and the client caches."""

    def __init__(self, db: _Db, backend: _Backend) -> None:
        self.db_pool = db
        self.trader = backend
        self.exchange_credentials: dict[str, Any] = {"binance": {"api_key": "k"}}
        self.exchange_credential_errors: dict[str, Any] = {}
        self._exchange_clients: dict[str, Any] = {"binance": _ClosingClient()}


class _ClosingClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Fakes: the exchange boundary (behind the real build_readonly_client)
# ---------------------------------------------------------------------------

RESTRICTIONS = {
    "ipRestrict": False,
    "enableWithdrawals": False,
    "enableSpotAndMarginTrading": True,
}


class FakeBinance:
    error: ClassVar[BaseException | None] = None
    restrictions: ClassVar[dict[str, Any]] = dict(RESTRICTIONS)
    instances: ClassVar[list[FakeBinance]] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.closed = False
        self.restriction_calls = 0
        FakeBinance.instances.append(self)

    async def close(self) -> None:
        self.closed = True


async def _fake_get_account_api_restrictions(fake: FakeBinance, *args: Any) -> dict[str, Any]:
    fake.restriction_calls += 1
    if FakeBinance.error is not None:
        raise FakeBinance.error
    return dict(FakeBinance.restrictions)


# ccxt's camelCase implicit API name, bound as an attribute (pep8-naming flags the def)
FakeBinance.sapiGetAccountApiRestrictions = _fake_get_account_api_restrictions


@pytest.fixture()
def binance(monkeypatch: pytest.MonkeyPatch) -> type[FakeBinance]:
    FakeBinance.error = None
    FakeBinance.restrictions = dict(RESTRICTIONS)
    FakeBinance.instances = []
    monkeypatch.setattr(ccxt.async_support, "binance", FakeBinance)
    return FakeBinance


BYBIT_KEY_INFO: dict[str, Any] = {
    "retCode": 0,
    "retMsg": "",
    "result": {
        "id": "42",
        "note": "hydra",
        "readOnly": 0,
        "permissions": {
            "ContractTrade": [],
            "Spot": ["SpotTrade"],
            "Wallet": ["AccountTransfer"],
        },
        "ips": [],
    },
}


class FakeBybit:
    error: ClassVar[BaseException | None] = None
    key_info: ClassVar[dict[str, Any]] = dict(BYBIT_KEY_INFO)
    instances: ClassVar[list[FakeBybit]] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.closed = False
        self.query_calls = 0
        FakeBybit.instances.append(self)

    async def close(self) -> None:
        self.closed = True


async def _fake_query_api(fake: FakeBybit, *args: Any) -> dict[str, Any]:
    fake.query_calls += 1
    if FakeBybit.error is not None:
        raise FakeBybit.error
    return dict(FakeBybit.key_info)


# ccxt's camelCase implicit API name, bound as an attribute (pep8-naming flags the def)
FakeBybit.privateGetV5UserQueryApi = _fake_query_api


@pytest.fixture()
def bybit(monkeypatch: pytest.MonkeyPatch) -> type[FakeBybit]:
    FakeBybit.error = None
    FakeBybit.key_info = dict(BYBIT_KEY_INFO)
    FakeBybit.instances = []
    monkeypatch.setattr(ccxt.async_support, "bybit", FakeBybit)
    return FakeBybit


@pytest.fixture(autouse=True)
def credential_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())


def _creds_row(
    *,
    environment: str = "mainnet",
    permissions: str | None = json.dumps(
        {"enable_spot_and_margin_trading": True, "enable_withdrawals": False, "ip_restrict": False}
    ),
) -> dict[str, Any]:
    return {
        "encrypted_key": encrypt("key-1"),
        "encrypted_secret": encrypt("secret-1"),
        "encrypted_passphrase": None,
        "validated_environment": environment,
        "validated_permissions": permissions,
    }


def _detail(exc: ServiceError) -> dict[str, Any]:
    assert isinstance(exc.detail, dict)
    return exc.detail


async def _arm(db: _Db, backend: _Backend, **params: Any) -> dict[str, Any]:
    body = {"live_capital": 500.0, "reason": "arm live"}
    body.update(params)
    return await execute_live_arming(_Deps(db, backend), body, "user:soma")


# ---------------------------------------------------------------------------
# The armed row
# ---------------------------------------------------------------------------


class TestArmedState:
    async def test_absent_or_broken_rows_read_disarmed(self) -> None:
        assert await read_live_state(None) == {
            "armed": False,
            "armed_by": None,
            "armed_at": None,
        }
        assert (await read_live_state(_Db()))["armed"] is False
        assert (await read_live_state(_BrokenPool()))["armed"] is False

    async def test_armed_row_reads_back_with_its_attribution(self) -> None:
        db = _Db(config={"live_armed": ARMED_ROW})
        assert await read_live_state(db) == {
            "armed": True,
            "armed_by": "user:soma",
            "armed_at": "2026-10-28T00:00:00+00:00",
        }

    async def test_disarmed_row_reads_disarmed_with_null_attribution(self) -> None:
        db = _Db(config={"live_armed": json.dumps({"armed": False, "by": "user:soma", "at": "x"})})
        assert await read_live_state(db) == {
            "armed": False,
            "armed_by": None,
            "armed_at": None,
        }

    async def test_live_capital_read(self) -> None:
        db = _Db(config={"live_capital": "500.0"})
        assert await read_live_capital(db) == 500.0
        assert await read_live_capital(_Db()) is None
        assert await read_live_capital(None) is None
        assert await read_live_capital(_Db(config={"live_capital": '"-1"'})) is None

    async def test_write_armed_transition_writes_row_and_event_together(self) -> None:
        db = _Db(config={"live_armed": ARMED_ROW})
        await write_armed_transition(db, armed=False, actor="user:soma", reason="off")
        assert db.value("live_armed")["armed"] is False
        assert db.value("live_armed")["by"] == "user:soma"
        assert db.events == [
            {
                "scope": "paper",
                "kind": "live_disarmed",
                "actor": "user:soma",
                "reason": "off",
                "details": {},
            }
        ]

    async def test_a_broken_write_raises_and_writes_nothing(self) -> None:
        with pytest.raises(ServiceError) as exc:
            await write_armed_transition(_BrokenPool(), armed=True, actor="a", reason="r")
        assert _detail(exc.value)["code"] == "database_unavailable"


class TestPermissions:
    def test_normalize_restrictions_maps_the_binance_flags(self) -> None:
        assert normalize_restrictions("binance", RESTRICTIONS) == {
            "enable_spot_and_margin_trading": True,
            "enable_withdrawals": False,
            "ip_restrict": False,
        }
        assert normalize_restrictions("binance", {}) == {
            "enable_spot_and_margin_trading": False,
            "enable_withdrawals": False,
            "ip_restrict": False,
        }
        assert normalize_restrictions("binance", None)["enable_spot_and_margin_trading"] is False

    def test_normalize_restrictions_maps_the_bybit_query_api_answer(self) -> None:
        """GET /v5/user/query-api (the Bybit apikey-info shape, as ccxt passes it raw):
        spot trading is 'SpotTrade' in permissions['Spot'], withdrawals are 'Withdraw'
        in permissions['Wallet'], the IP restriction is a specific (non-'*') ips entry."""
        assert normalize_restrictions(
            "bybit",
            {
                "retCode": 0,
                "result": {
                    "id": "2208369",
                    "readOnly": 0,
                    "permissions": {
                        "ContractTrade": ["Order", "Position"],
                        "Spot": ["SpotTrade"],
                        "Wallet": ["AccountTransfer"],
                        "Options": [],
                        "Derivatives": [],
                    },
                    "ips": ["91.0.0.1"],
                },
            },
        ) == {
            "enable_spot_and_margin_trading": True,
            "enable_withdrawals": False,
            "ip_restrict": True,
        }
        # a withdraw-capable key, an unrestricted ips list and the legacy permission
        # names (either account generation can sit behind the same key) all read honestly
        assert normalize_restrictions(
            "bybit",
            {
                "result": {
                    "permissions": {
                        "Spot": [],
                        "Wallet": ["AccountTransfer", "Withdraw"],
                    },
                    "ips": ["*"],
                }
            },
        ) == {
            "enable_spot_and_margin_trading": False,
            "enable_withdrawals": True,
            "ip_restrict": False,
        }
        assert (
            normalize_restrictions(
                "bybit",
                {"result": {"permissions": {"SpotTrading": ["AccountRead", "AccountTrade"]}}},
            )["enable_spot_and_margin_trading"]
            is True
        )
        assert (
            normalize_restrictions("bybit", {"retCode": 10003})["enable_spot_and_margin_trading"]
            is False
        )
        assert normalize_restrictions("bybit", None)["enable_withdrawals"] is False

    def test_stored_permissions_parses_jsonb_text(self) -> None:
        raw = json.dumps({"enable_spot_and_margin_trading": True})
        assert stored_permissions(raw) == {"enable_spot_and_margin_trading": True}
        assert stored_permissions(None) is None
        assert stored_permissions("not json") is None
        assert stored_permissions("[1]") is None

    def test_is_trade_capable(self) -> None:
        assert is_trade_capable(json.dumps({"enable_spot_and_margin_trading": True}))
        assert not is_trade_capable(json.dumps({"enable_spot_and_margin_trading": False}))
        assert not is_trade_capable(None)
        # P4-F: a binanceusdm key that can trade futures is trade-capable for the delete gate.
        assert is_trade_capable(
            json.dumps({"enable_spot_and_margin_trading": False, "enable_futures": True})
        )
        assert not is_trade_capable(json.dumps({"enable_futures": False}))


# ---------------------------------------------------------------------------
# require_live_capable_credentials (R8: the re-fetch)
# ---------------------------------------------------------------------------


class TestRequireLiveCapableCredentials:
    async def test_no_stored_keys_is_403(self, binance: type[FakeBinance]) -> None:
        with pytest.raises(ServiceError) as exc:
            await require_live_capable_credentials(_Db())
        assert (_detail(exc.value)["code"], exc.value.status_code) == (
            "live_credentials_missing",
            403,
        )
        assert FakeBinance.instances == []

    async def test_undecryptable_keys_are_403(self, binance: type[FakeBinance]) -> None:
        db = _Db(credentials={"binance": {**_creds_row(), "encrypted_key": "not-fernet"}})
        with pytest.raises(ServiceError) as exc:
            await require_live_capable_credentials(db)
        assert _detail(exc.value)["code"] == "live_credentials_missing"

    async def test_testnet_keys_are_refused(self, binance: type[FakeBinance]) -> None:
        db = _Db(credentials={"binance": _creds_row(environment="testnet")})
        with pytest.raises(ServiceError) as exc:
            await require_live_capable_credentials(db)
        assert (_detail(exc.value)["code"], exc.value.status_code) == (
            "credentials_not_live_capable",
            409,
        )
        assert FakeBinance.instances == []  # never reached the exchange

    async def test_trade_only_keys_pass_and_are_refetched(self, binance: type[FakeBinance]) -> None:
        db = _Db(credentials={"binance": _creds_row()})
        assert await require_live_capable_credentials(db) == {
            "binance": {
                "enable_spot_and_margin_trading": True,
                "enable_withdrawals": False,
                "ip_restrict": False,
            }
        }
        [probe] = FakeBinance.instances
        assert probe.restriction_calls == 1  # R8: the live re-fetch, not the snapshot
        assert probe.opts["apiKey"] == "key-1"
        assert "sandbox" not in probe.opts  # the mainnet client
        assert probe.closed is True

    async def test_withdrawals_enabled_now_refuses_arming(self, binance: type[FakeBinance]) -> None:
        """R8: the snapshot says trade-only, the LIVE re-fetch says withdrawals on."""
        FakeBinance.restrictions = {**RESTRICTIONS, "enableWithdrawals": True}
        db = _Db(
            credentials={
                "binance": _creds_row(
                    permissions=json.dumps(
                        {
                            "enable_spot_and_margin_trading": True,
                            "enable_withdrawals": False,
                            "ip_restrict": False,
                        }
                    )
                )
            }
        )
        with pytest.raises(ServiceError) as exc:
            await require_live_capable_credentials(db)
        assert _detail(exc.value)["code"] == "credentials_not_live_capable"
        assert "withdraw" in _detail(exc.value)["message"]

    async def test_spot_disabled_now_refuses_arming(self, binance: type[FakeBinance]) -> None:
        FakeBinance.restrictions = {**RESTRICTIONS, "enableSpotAndMarginTrading": False}
        with pytest.raises(ServiceError) as exc:
            await require_live_capable_credentials(_Db(credentials={"binance": _creds_row()}))
        assert _detail(exc.value)["code"] == "credentials_not_live_capable"

    @pytest.mark.parametrize(
        "error", [ccxt.NetworkError("dns"), ccxt.AuthenticationError("revoked"), TimeoutError()]
    )
    async def test_an_unreadable_re_fetch_refuses_arming(
        self, binance: type[FakeBinance], error: BaseException
    ) -> None:
        FakeBinance.error = error
        with pytest.raises(ServiceError) as exc:
            await require_live_capable_credentials(_Db(credentials={"binance": _creds_row()}))
        assert _detail(exc.value)["code"] == "credentials_not_live_capable"


# ---------------------------------------------------------------------------
# The arm loop: every CONNECTED supported spot venue is re-checked (P7-F)
# ---------------------------------------------------------------------------


class TestTheArmLoop:
    async def test_an_unconnected_bybit_never_blocks_arming_binance(
        self, binance: type[FakeBinance], bybit: type[FakeBybit]
    ) -> None:
        """An unconnected venue is skipped: only binance's key is probed."""
        checked = await require_live_capable_credentials(_Db(credentials={"binance": _creds_row()}))
        assert checked == {
            "binance": {
                "enable_spot_and_margin_trading": True,
                "enable_withdrawals": False,
                "ip_restrict": False,
            }
        }
        assert FakeBybit.instances == []

    async def test_bybit_trade_only_keys_pass_and_are_refetched(
        self, binance: type[FakeBinance], bybit: type[FakeBybit]
    ) -> None:
        db = _Db(credentials={"binance": _creds_row(), "bybit": _creds_row()})
        checked = await require_live_capable_credentials(db)
        assert checked["bybit"] == {
            "enable_spot_and_margin_trading": True,
            "enable_withdrawals": False,
            "ip_restrict": False,
        }
        [probe] = FakeBybit.instances
        assert probe.query_calls == 1  # the v5 query-api re-fetch, not the snapshot
        assert "sandbox" not in probe.opts  # the mainnet client
        assert probe.closed is True

    async def test_a_connected_bybit_key_with_withdraw_refuses_the_arm(
        self, binance: type[FakeBinance], bybit: type[FakeBybit]
    ) -> None:
        """The fail-closed arm: ANY connected venue's key that can withdraw refuses the
        arm for all of them, naming the venue."""
        FakeBybit.key_info = {
            "retCode": 0,
            "result": {
                "permissions": {"Spot": ["SpotTrade"], "Wallet": ["AccountTransfer", "Withdraw"]},
                "ips": [],
            },
        }
        db = _Db(credentials={"binance": _creds_row(), "bybit": _creds_row()})
        with pytest.raises(ServiceError) as exc:
            await require_live_capable_credentials(db)
        assert _detail(exc.value)["code"] == "credentials_not_live_capable"
        assert "bybit key has withdrawals enabled" in _detail(exc.value)["message"]

    async def test_a_connected_bybit_key_that_cannot_trade_refuses_the_arm(
        self, binance: type[FakeBinance], bybit: type[FakeBybit]
    ) -> None:
        FakeBybit.key_info = {
            "retCode": 0,
            "result": {"permissions": {"Spot": [], "Wallet": ["AccountTransfer"]}, "ips": []},
        }
        db = _Db(credentials={"binance": _creds_row(), "bybit": _creds_row()})
        with pytest.raises(ServiceError) as exc:
            await require_live_capable_credentials(db)
        assert _detail(exc.value)["code"] == "credentials_not_live_capable"
        assert "bybit key cannot trade spot" in _detail(exc.value)["message"]

    async def test_bybit_testnet_keys_refuse_the_arm(
        self, binance: type[FakeBinance], bybit: type[FakeBybit]
    ) -> None:
        db = _Db(
            credentials={
                "binance": _creds_row(),
                "bybit": _creds_row(environment="testnet"),
            }
        )
        with pytest.raises(ServiceError) as exc:
            await require_live_capable_credentials(db)
        assert _detail(exc.value)["code"] == "credentials_not_live_capable"
        assert "bybit credentials are not validated for mainnet" in _detail(exc.value)["message"]
        assert FakeBybit.instances == []  # never reached the exchange

    async def test_a_bybit_outage_refuses_the_arm_for_every_venue(
        self, binance: type[FakeBinance], bybit: type[FakeBybit]
    ) -> None:
        """Fail-closed by design (documented): a connected venue whose permissions
        cannot be re-read refuses the arm for binance too."""
        FakeBybit.error = ccxt.NetworkError("dns")
        db = _Db(credentials={"binance": _creds_row(), "bybit": _creds_row()})
        with pytest.raises(ServiceError) as exc:
            await require_live_capable_credentials(db)
        assert _detail(exc.value)["code"] == "credentials_not_live_capable"
        assert "bybit key permissions could not be re-validated" in _detail(exc.value)["message"]

    async def test_no_stored_keys_on_any_venue_is_403(
        self, binance: type[FakeBinance], bybit: type[FakeBybit]
    ) -> None:
        with pytest.raises(ServiceError) as exc:
            await require_live_capable_credentials(_Db())
        assert (_detail(exc.value)["code"], exc.value.status_code) == (
            "live_credentials_missing",
            403,
        )
        assert FakeBinance.instances == [] and FakeBybit.instances == []


# ---------------------------------------------------------------------------
# The live_arming executor
# ---------------------------------------------------------------------------


class TestLiveArmingExecutor:
    async def test_arms_and_writes_the_capital_as_one_transition(
        self, binance: type[FakeBinance]
    ) -> None:
        db = _Db(credentials={"binance": _creds_row()})
        result = await _arm(db, _Backend())
        assert result["status"] == "armed"
        assert result["live_capital"] == 500.0
        assert db.value("live_armed") == {
            "armed": True,
            "by": "user:soma",
            "at": result["armed_at"],
        }
        assert db.value("live_capital") == 500.0
        assert db.events == [
            {
                "scope": "paper",
                "kind": "live_armed",
                "actor": "user:soma",
                "reason": "arm live",
                "details": {"live_capital": 500.0},
            }
        ]

    async def test_the_kill_switch_refuses_arming(self, binance: type[FakeBinance]) -> None:
        db = _Db(credentials={"binance": _creds_row()})
        with pytest.raises(ServiceError) as exc:
            await _arm(db, _Backend(kill_switch=True))
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("kill_switch_active", 409)
        assert "live_armed" not in db.config
        assert FakeBinance.instances == []

    async def test_bad_keys_refuse_arming_before_any_write(
        self, binance: type[FakeBinance]
    ) -> None:
        db = _Db()  # no stored credentials
        with pytest.raises(ServiceError) as exc:
            await _arm(db, _Backend())
        assert _detail(exc.value)["code"] == "live_credentials_missing"
        assert "live_armed" not in db.config
        assert db.events == []

    async def test_already_armed_is_a_clean_no_op(self, binance: type[FakeBinance]) -> None:
        db = _Db(
            config={"live_armed": ARMED_ROW, "live_capital": "250.0"},
            credentials={"binance": _creds_row()},
        )
        result = await _arm(db, _Backend(), live_capital=999.0)
        assert result["status"] == "already_armed"
        assert result["live_capital"] == 250.0  # the capital is not silently changed
        assert db.value("live_armed")["by"] == "user:soma"  # the original arming stands
        assert db.value("live_capital") == 250.0
        assert db.events == []

    async def test_a_bad_live_capital_is_refused(self, binance: type[FakeBinance]) -> None:
        db = _Db(credentials={"binance": _creds_row()})
        for bad in (None, 0, -1, "nan"):
            with pytest.raises(ServiceError) as exc:
                await _arm(db, _Backend(), live_capital=bad)
            assert _detail(exc.value)["code"] == "invalid_params"
        assert "live_armed" not in db.config


# ---------------------------------------------------------------------------
# The session_start / order / credential_delete executors
# ---------------------------------------------------------------------------


def _live_session(session_id: str = "s-live", status: str = "running") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "trading_mode": "live",
        "status": status,
        "strategy_id": "momentum",
    }


class TestSessionStartExecutor:
    async def test_starts_through_the_backend_with_live_mode(
        self, binance: type[FakeBinance]
    ) -> None:
        db = _Db(config={"live_armed": ARMED_ROW})
        backend = _Backend()
        result = await execute_live_session_start(
            _Deps(db, backend),
            {"strategy_id": "momentum", "paper_capital": 250.0, "reason": "go live"},
            "user:soma",
        )
        assert result["created_by"] == "user:soma"
        [(actor, body)] = backend.start_calls
        assert (actor, body.trading_mode, body.paper_capital) == ("user:soma", "live", 250.0)

    async def test_disarmed_between_create_and_approve_is_refused(self) -> None:
        """The executor re-validates at execution time (R22 surfaces the refusal)."""
        db = _Db()  # the armed row never existed / was removed while the approval sat
        with pytest.raises(ServiceError) as exc:
            await execute_live_session_start(
                _Deps(db, _Backend()), {"strategy_id": "momentum", "reason": "go live"}, "user:soma"
            )
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("live_not_armed", 409)

    async def test_executed_after_the_kill_switch_latched_is_refused(self) -> None:
        db = _Db(config={"live_armed": ARMED_ROW})
        with pytest.raises(ServiceError) as exc:
            await execute_live_session_start(
                _Deps(db, _Backend(kill_switch=True)),
                {"strategy_id": "momentum", "reason": "go live"},
                "user:soma",
            )
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("kill_switch_active", 409)

    async def test_a_bad_paper_capital_is_422_invalid_params(self) -> None:
        """R31 A4r: like live_order, a bad param answers 422, not the flattened 503."""
        db = _Db(config={"live_armed": ARMED_ROW})
        with pytest.raises(ServiceError) as exc:
            await execute_live_session_start(
                _Deps(db, _Backend()),
                {"strategy_id": "momentum", "paper_capital": "abc", "reason": "go live"},
                "user:soma",
            )
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("invalid_params", 422)


class TestLiveOrderExecutor:
    async def test_orders_through_the_backend_on_a_live_running_session(self) -> None:
        db = _Db(config={"live_armed": ARMED_ROW})
        backend = _Backend(sessions=[_live_session()])
        params = {
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "MARKET",
            "quantity": "0.01",
            "session_id": "s-live",
            "reason": "manual live order",
        }
        result = await execute_live_order(_Deps(db, backend), params, "user:soma")
        assert result["order_id"] == "o1"
        [order] = backend.order_calls
        assert (order.symbol, order.quantity, order.session_id) == (
            "BTCUSDT",
            Decimal("0.01"),
            "s-live",
        )

    async def test_executed_after_the_kill_switch_latched_is_refused(self) -> None:
        """The sibling re-validation session_start has (R31 A2o): a latched switch
        never places a manual order through the executor path."""
        db = _Db(config={"live_armed": ARMED_ROW})
        backend = _Backend(kill_switch=True, sessions=[_live_session()])
        with pytest.raises(ServiceError) as exc:
            await execute_live_order(
                _Deps(db, backend),
                {"session_id": "s-live", "side": "buy", "quantity": "0.01", "reason": "r"},
                "user:soma",
            )
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("kill_switch_active", 409)
        assert backend.order_calls == []  # nothing reached the wire

    async def test_disarmed_is_refused(self) -> None:
        with pytest.raises(ServiceError) as exc:
            await execute_live_order(
                _Deps(_Db(), _Backend(sessions=[_live_session()])),
                {"session_id": "s-live", "reason": "r"},
                "user:soma",
            )
        assert _detail(exc.value)["code"] == "live_not_armed"

    async def test_a_paper_or_stopped_session_is_not_a_live_session(self) -> None:
        db = _Db(config={"live_armed": ARMED_ROW})

        async def _refuse(
            sessions: list[dict[str, Any]], session_id: str, code: str, status: int
        ) -> None:
            with pytest.raises(ServiceError) as exc:
                await execute_live_order(
                    _Deps(db, _Backend(sessions=sessions)),
                    {"session_id": session_id, "side": "buy", "quantity": "0.01", "reason": "r"},
                    "user:soma",
                )
            assert (_detail(exc.value)["code"], exc.value.status_code) == (code, status)

        await _refuse(
            [{**_live_session(), "trading_mode": "paper"}], "s-live", "not_a_live_session", 409
        )
        await _refuse([_live_session(status="stopped")], "s-live", "not_a_live_session", 409)
        await _refuse([], "s-live", "session_not_found", 404)


class TestCredentialDeleteExecutor:
    async def test_deletes_the_row_and_evicts_the_cached_client(self) -> None:
        db = _Db(credentials={"binance": _creds_row()})
        deps = _Deps(db, _Backend())
        client = deps._exchange_clients["binance"]
        result = await execute_credential_delete(
            deps, {"exchange_id": "binance", "reason": "rotate keys"}, "user:soma"
        )
        assert result == {
            "exchange_id": "binance",
            "deleted": True,
            "message": "Disconnected from binance",
        }
        assert "binance" not in db.credentials
        assert "binance" not in deps.exchange_credentials
        assert "binance" not in deps._exchange_clients
        assert client.closed is True

    async def test_nothing_stored_is_404(self) -> None:
        with pytest.raises(ServiceError) as exc:
            await execute_credential_delete(
                _Deps(_Db(), _Backend()), {"exchange_id": "binance", "reason": "r"}, "user:soma"
            )
        assert (exc.value.status_code, _detail(exc.value)["code"]) == (404, "credentials_not_found")

    async def test_a_running_live_session_gates_the_delete(self) -> None:
        """Must-fix #5: the trader's keyed pool entry outlives an API-side evict."""
        db = _Db(credentials={"binance": _creds_row()})
        backend = _Backend(sessions=[_live_session()])
        with pytest.raises(ServiceError) as exc:
            await execute_credential_delete(
                _Deps(db, backend), {"exchange_id": "binance", "reason": "r"}, "user:soma"
            )
        assert (_detail(exc.value)["code"], exc.value.status_code) == (
            "credential_delete_gated",
            409,
        )
        assert _detail(exc.value)["running_live_sessions"] == ["s-live"]
        assert "binance" in db.credentials  # nothing was deleted


# ---------------------------------------------------------------------------
# create_live_approval and the store wiring
# ---------------------------------------------------------------------------


class TestCreateLiveApproval:
    async def test_identical_retry_dedupes_into_the_same_approval(self) -> None:
        store = live_approval_store(_Deps(_Db(), _Backend()))
        first = await create_live_approval(
            store,
            action="live_arming",
            reason="arm",
            params={"live_capital": 500.0, "reason": "arm"},
            requested_by="user:soma",
        )
        again = await create_live_approval(
            store,
            action="live_arming",
            reason="arm",
            params={"live_capital": 500.0, "reason": "arm"},
            requested_by="user:soma",
        )
        assert again.id == first.id

    async def test_different_params_conflict(self) -> None:
        store = live_approval_store(_Deps(_Db(), _Backend()))
        await create_live_approval(
            store,
            action="live_arming",
            reason="arm",
            params={"live_capital": 500.0, "reason": "arm"},
            requested_by="user:soma",
        )
        with pytest.raises(ServiceError) as exc:
            await create_live_approval(
                store,
                action="live_arming",
                reason="arm more",
                params={"live_capital": 250.0, "reason": "arm more"},
                requested_by="user:soma",
            )
        assert (_detail(exc.value)["code"], exc.value.status_code) == ("approval_conflict", 409)
        assert len(await store.list_pending()) == 1

    async def test_registration_is_idempotent_and_shared(self) -> None:
        deps = _Deps(_Db(), _Backend())
        store = live_approval_store(deps)
        again = live_approval_store(deps)
        assert again is store
        assert isinstance(store, ApprovalStore)


def test_the_live_actions_are_the_four_executors() -> None:
    assert live_gate.LIVE_ACTIONS == (
        "live_arming",
        "live_session_start",
        "live_order",
        "credential_delete",
    )
