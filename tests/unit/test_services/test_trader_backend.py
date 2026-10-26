"""The TraderBackend seam: LocalTrader implements exactly the pinned op set.

Adding or removing an op anywhere — the protocol or LocalTrader — fails here, so the seam
D5's RemoteTrader and the split trader server implement cannot drift silently. The op set
is frozen for all of P3 (pinned seam #5): additive optional parameters only, no new ops.
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
