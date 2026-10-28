"""The no-op cache sweeper: no smoke ever runs real deletions by accident.

``no_op_sweeper_factory`` is installed on ``app.state.data_cache_sweeper_factory``
(the ``health_monitor_factory`` pattern) by the smokes. The real sweeper's first
cycle runs at boot against the smoke's real pool, so without this seam a future
smoke that seeds auto-fetched bars could lose them to a real eviction.
"""

from __future__ import annotations

from typing import Any


class NoOpCacheSweeper:
    """The sweeper's lifecycle seam with no background loop and no deletes."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def sweep_once(self, pool: Any) -> tuple[int, int]:
        return (0, 0)


def no_op_sweeper_factory(app: Any) -> NoOpCacheSweeper:
    """The factory the smokes install so the real sweeper never starts."""
    return NoOpCacheSweeper()
