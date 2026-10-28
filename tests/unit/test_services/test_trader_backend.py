"""The TraderBackend seam: the three implementations implement exactly the pinned op set.

Adding or removing an op anywhere — the protocol, ``LocalTrader``, ``RemoteTrader`` (D5) or
the trader server's routes — fails here, so the seam D5 carries across the process boundary
cannot drift silently. The op set is frozen for all of P3 (pinned seam #5): additive optional
parameters only, no new ops.
"""

from __future__ import annotations

import inspect
from typing import Any

from hydra.services.trader import LocalTrader, TraderBackend


def _ops(cls: type[Any]) -> dict[str, inspect.Signature]:
    """The public callables declared on *cls* (properties and dunders are not ops)."""
    return {
        name: inspect.signature(member)
        for name, member in vars(cls).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }


class TestTraderBackendSeam:
    def test_local_trader_satisfies_the_protocol(self) -> None:
        assert issubclass(LocalTrader, TraderBackend)

    def test_local_trader_implements_exactly_the_protocol_ops(self) -> None:
        assert set(_ops(LocalTrader)) == set(_ops(TraderBackend))

    def test_every_op_matches_the_protocol_signature(self) -> None:
        protocol = _ops(TraderBackend)
        for name, local in _ops(LocalTrader).items():
            assert str(local) == str(protocol[name]), f"{name} drifted from the protocol"

    def test_the_op_count_is_pinned(self) -> None:
        assert len(_ops(TraderBackend)) == 19

    def test_remote_trader_implements_exactly_the_protocol_ops(self) -> None:
        """Plus its own HTTP lifecycle close — every other public callable is a seam op."""
        from hydra.dashboard.trader_client import RemoteTrader

        assert set(_ops(RemoteTrader)) - {"aclose"} == set(_ops(TraderBackend))

    def test_every_remote_op_matches_the_protocol_signature(self) -> None:
        from hydra.dashboard.trader_client import RemoteTrader

        protocol = _ops(TraderBackend)
        for name, remote in _ops(RemoteTrader).items():
            if name == "aclose":
                continue
            assert str(remote) == str(protocol[name]), f"{name} drifted from the protocol"

    def test_the_trader_server_serves_exactly_the_protocol_ops(self) -> None:
        """One route per op: the server's /ops handlers are named after the ops, no extras."""
        from hydra.trader.server import ops_router

        served = {route.endpoint.__name__ for route in ops_router.routes}
        assert served == set(_ops(TraderBackend))
