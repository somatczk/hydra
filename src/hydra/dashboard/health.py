"""Engine liveness and readiness.

``HealthMonitor`` runs one readiness check at startup and then every ``interval_s`` in the
background. Each run sets ``hydra_ready{check}``, ``hydra_session_desired`` /
``hydra_session_supervised`` and ``hydra_health_monitor_last_run_timestamp_seconds``, so the
Prometheus rules can alert on a failing check, a zombie session or a stalled monitor.

- Liveness (``/livez``): startup completed and the background loop ran within 3 intervals.
- Readiness (``/readyz``): the DB answers ``SELECT 1``, the schema is at the Alembic head, every
  session the DB marks running has a live task here (a desired session without one is a zombie,
  OP-2), every supervised session's feed is live and fresh, and the RiskService state is
  available. The kill switch is reported, not required. A probe gets the last evaluation while it
  is at most ``CACHE_INTERVALS`` intervals old, else one evaluation runs (one at a time); only the
  background run writes the gauges.

Two check sets (D5): the engine-side set (``REQUIRED_CHECKS``: db, migrations, sessions, feeds,
risk — the full set the trader process and the single-process app run, with the engine reads
routed through the backend) and the split-API set (``SPLIT_CHECKS``: db, migrations, trader),
where the ``trader`` check embeds the remote trader's own ``/readyz`` body — the API process
beside a remote trader has no engine to check. While the opt-in event fan-out is on
(``events.bus: redis``), the engine-side set additionally requires ``redis`` (the check exists
exactly when the dependency exists); the split API's set is unchanged — its trader check embeds
the trader's own redis result.

Feed freshness uses the same thresholds as ``HydraFeedStale`` and ``HydraClosedBarOverdue``,
measured against the monitor's clock from the feed gauges.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from prometheus_client import REGISTRY

import hydra
from hydra.core import metrics
from hydra.data.bars import timeframe_seconds
from hydra.data.migrations.runner import head_revision

if TYPE_CHECKING:
    from fastapi import FastAPI

    from hydra.execution.session_manager import HealthSnapshot, SessionHealth

logger = logging.getLogger(__name__)

HEALTH_INTERVAL_S = 15.0
LIVENESS_INTERVALS = 3
DB_TIMEOUT_S = 2.0
SNAPSHOT_TIMEOUT_S = 5.0
CACHE_INTERVALS = 2
FEED_MESSAGE_MAX_AGE_S = 120.0
CLOSED_BAR_GRACE_S = 60.0
CLOSED_BAR_MAX_BARS = 2

REQUIRED_CHECKS = ("db", "migrations", "sessions", "feeds", "risk")
SPLIT_CHECKS = ("db", "migrations", "trader")

type Clock = Callable[[], float]
type SessionKey = tuple[str, str, str]


def closed_bar_max_age(interval_s: float) -> float:
    """Oldest acceptable open time of the last closed bar, in seconds (rule: 2 bars + 60s)."""
    return CLOSED_BAR_MAX_BARS * interval_s + CLOSED_BAR_GRACE_S


@dataclass(frozen=True, slots=True)
class Readiness:
    """One readiness evaluation; ``checks`` is the public ``/readyz`` body section."""

    ready: bool
    checked_at: float
    checks: dict[str, dict[str, Any]]
    sessions_known: bool = True

    @property
    def zombies(self) -> list[str]:
        return list(self.checks["sessions"].get("zombies", []))

    def body(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.ready else "not_ready",
            "version": hydra.__version__,
            "checks": self.checks,
        }


def _error(exc: BaseException) -> str:
    return "timeout" if isinstance(exc, TimeoutError) else type(exc).__name__


def _gauge_value(name: str, labels: dict[str, str]) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


def _holds_state(health: SessionHealth) -> bool:
    """A stopped or errored session whose position or unpersisted fills still need watching."""
    return health.open_positions > 0 or health.persist_dirty or health.flatten_incomplete


class HealthMonitor:
    """Background readiness checks for one app (``app.state.health_monitor``).

    ``checks`` selects the set: ``REQUIRED_CHECKS`` (default — the engine-side full set)
    or ``SPLIT_CHECKS``. Unset, it follows the app's config: an app whose
    ``trader.url`` is set runs beside a remote trader and has no engine of its own, so it
    runs the split set with the remote-trader check; an engine-side app whose
    ``events.bus`` is ``redis`` additionally requires the redis check (the split set never
    grows it — the trader check embeds the trader's own).
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        interval_s: float = HEALTH_INTERVAL_S,
        clock: Clock = time.time,
        monotonic: Clock = time.monotonic,
        checks: tuple[str, ...] | None = None,
    ) -> None:
        self._app = app
        self._interval_s = interval_s
        self._clock = clock
        self._monotonic = monotonic
        if checks is None:
            cfg = getattr(app.state, "config", None)
            url = getattr(getattr(cfg, "trader", None), "url", None)
            if url is not None:
                checks = SPLIT_CHECKS
            elif getattr(getattr(cfg, "events", None), "bus", "memory") == "redis":
                checks = (*REQUIRED_CHECKS, "redis")
            else:
                checks = REQUIRED_CHECKS
        self._checks = checks
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._last_run: float | None = None
        self._published: set[SessionKey] = set()
        self._first_supervised: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.last: Readiness | None = None

    @property
    def interval_s(self) -> float:
        return self._interval_s

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run once inline (so ``/livez`` is green when startup returns), then loop."""
        await self.run_once()
        self._task = asyncio.create_task(self._loop(), name="health-monitor")
        self._started = True

    async def stop(self) -> None:
        self._started = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def alive(self) -> bool:
        """Startup completed and the loop ran within ``LIVENESS_INTERVALS`` intervals."""
        if not self._started or self._last_run is None:
            return False
        if self._task is None or self._task.done():
            return False
        return self._monotonic() - self._last_run <= LIVENESS_INTERVALS * self._interval_s

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Health monitor run failed")

    async def run_once(self) -> Readiness:
        """One background run: check, publish the gauges, record the heartbeat."""
        async with self._lock:
            result = await self.check()
            self.last = result
            self._publish_ready(result)
        self._last_run = self._monotonic()
        metrics.mark_health_monitor_run(at=self._clock())
        return result

    def fresh(self) -> Readiness | None:
        """The last result while at most ``CACHE_INTERVALS`` intervals old, else None."""
        last = self.last
        if last is None:
            return None
        age = self._clock() - last.checked_at
        return last if 0 <= age <= CACHE_INTERVALS * self._interval_s else None

    async def readiness(self) -> Readiness:
        """The last evaluation while fresh, else a new one; never writes the gauges."""
        cached = self.fresh()
        if cached is not None:
            return cached
        async with self._lock:
            cached = self.fresh()
            if cached is not None:
                return cached
            result = await self.check()
            self.last = result
            return result

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def _engine(self) -> Any:
        """The engine handle, routed through the backend (A6): the seam, not the state.

        A bound backend (the lifespan binds ``LocalTrader`` beside the engine, and the
        trader's own app binds one) owns the engine; the direct ``session_manager`` read
        stays as the fallback for the tests that bind the manager alone.
        """
        backend = getattr(self._app.state, "trader", None)
        mgr = getattr(backend, "session_manager", None)
        if mgr is not None:
            return mgr
        return getattr(self._app.state, "session_manager", None)

    async def check(self) -> Readiness:
        now = self._clock()
        pool = getattr(self._app.state, "db_pool", None)
        checks: dict[str, dict[str, Any]] = {
            "db": await self._check_db(pool),
            "migrations": await self._check_migrations(pool),
        }
        if "redis" in self._checks:
            checks["redis"] = await self._check_redis()
        snap: HealthSnapshot | None = None
        if "trader" in self._checks:
            checks["trader"] = await self._check_trader()
        else:
            engine = self._engine()
            snap_error: str | None = None
            if engine is None:
                snap_error = "session manager not started"
            else:
                try:
                    snap = await asyncio.wait_for(engine.health_snapshot(), SNAPSHOT_TIMEOUT_S)
                except Exception as exc:
                    logger.warning("Health snapshot failed", exc_info=True)
                    snap_error = _error(exc)
            checks["sessions"] = await self._check_sessions(snap, snap_error, pool)
            checks["feeds"] = self._check_feeds(snap, snap_error, now, engine)
            checks["risk"] = self._check_risk(snap, snap_error)
            checks["kill_switch"] = {
                "ok": snap is not None and not snap.kill_switch,
                "required": False,
                "active": None if snap is None else snap.kill_switch,
            }
        ready = all(checks[name]["ok"] for name in self._checks)
        return Readiness(
            ready=ready, checked_at=now, checks=checks, sessions_known=snap is not None
        )

    async def _check_db(self, pool: Any) -> dict[str, Any]:
        if pool is None:
            return {"ok": False, "required": True, "error": "no database configured"}
        started = time.perf_counter()
        try:
            value = await asyncio.wait_for(pool.fetchval("SELECT 1"), DB_TIMEOUT_S)
        except Exception as exc:
            return {"ok": False, "required": True, "error": _error(exc)}
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return {"ok": value == 1, "required": True, "latency_ms": latency_ms}

    async def _check_migrations(self, pool: Any) -> dict[str, Any]:
        head = head_revision()
        if pool is None:
            return {"ok": False, "required": True, "head": head, "error": "no database configured"}
        try:
            current = await asyncio.wait_for(
                pool.fetchval("SELECT version_num FROM alembic_version"), DB_TIMEOUT_S
            )
        except Exception as exc:
            return {"ok": False, "required": True, "head": head, "error": _error(exc)}
        return {"ok": current == head, "required": True, "current": current, "head": head}

    async def _check_redis(self) -> dict[str, Any]:
        """The event fan-out's Redis answers a PING (the DB_TIMEOUT_S pattern)."""
        cfg = getattr(self._app.state, "config", None)
        secret = getattr(cfg, "redis_url", None)
        url = secret.get_secret_value() if secret is not None else None
        if url is None:
            return {"ok": False, "required": True, "error": "no redis configured"}
        import redis.asyncio as aioredis

        started = time.perf_counter()
        try:
            client: Any = aioredis.from_url(url, decode_responses=False)
            try:
                await asyncio.wait_for(client.ping(), DB_TIMEOUT_S)
            finally:
                await client.aclose()
        except Exception as exc:
            return {"ok": False, "required": True, "error": _error(exc)}
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return {"ok": True, "required": True, "latency_ms": latency_ms}

    async def _check_sessions(
        self, snap: HealthSnapshot | None, error: str | None, pool: Any
    ) -> dict[str, Any]:
        if snap is None:
            return {"ok": False, "required": True, "error": error, "zombies": [], "sessions": []}
        items = [s for s in snap.sessions if s.desired or s.supervised or _holds_state(s)]
        zombies = sorted(s.session_id for s in items if s.desired and not s.supervised)
        orphans = sorted(s.session_id for s in items if s.supervised and not s.desired)
        labels = await self._zombie_labels(pool, [s for s in items if not s.strategy_id])
        out: dict[str, Any] = {
            "ok": snap.desired_from_db and not zombies and not orphans,
            "required": True,
            "desired_from_db": snap.desired_from_db,
            "zombies": zombies,
            "orphans": orphans,
            "sessions": [
                {
                    "session_id": s.session_id,
                    "strategy_id": s.strategy_id or labels.get(s.session_id, ("", ""))[0],
                    "mode": s.mode or labels.get(s.session_id, ("", ""))[1],
                    "desired": s.desired,
                    "supervised": s.supervised,
                }
                for s in sorted(items, key=lambda s: s.session_id)
            ],
        }
        if not snap.desired_from_db:
            out["error"] = "running sessions in the database are unreadable"
        return out

    async def _zombie_labels(
        self, pool: Any, unknown: list[SessionHealth]
    ) -> dict[str, tuple[str, str]]:
        """strategy_id and mode of DB-only sessions, for the per-session gauge labels."""
        if pool is None or not unknown:
            return {}
        try:
            rows = await asyncio.wait_for(
                pool.fetch(
                    "SELECT id, strategy_id, trading_mode FROM trading_sessions WHERE id = ANY($1)",
                    [s.session_id for s in unknown],
                ),
                DB_TIMEOUT_S,
            )
        except Exception:
            logger.warning("Zombie session labels unreadable", exc_info=True)
            return {}
        return {str(r["id"]): (str(r["strategy_id"]), str(r["trading_mode"])) for r in rows}

    async def _check_trader(self) -> dict[str, Any]:
        """The remote trader's own readiness, embedded verbatim (R27(5)/A6).

        A transport failure is an error string; an answer embeds the trader's
        ``/readyz`` body under ``detail`` whatever its status, so the API's 503 names
        the remote cause. The bearer token rides along when one is configured.
        """
        cfg = getattr(self._app.state, "config", None)
        url = getattr(getattr(cfg, "trader", None), "url", None)
        if url is None:
            return {"ok": False, "required": True, "error": "trader url not configured"}
        headers: dict[str, str] = {}
        token = getattr(cfg, "api_token", None)
        if token is not None:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        try:
            async with httpx.AsyncClient(timeout=DB_TIMEOUT_S) as client:
                response = await client.get(f"{url}/readyz", headers=headers)
        except Exception as exc:
            return {"ok": False, "required": True, "error": _error(exc)}
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        out: dict[str, Any] = {"ok": response.status_code == 200, "required": True}
        if body is not None:
            out["detail"] = body
        else:
            out["status_code"] = response.status_code
        return out

    def _check_feeds(
        self,
        snap: HealthSnapshot | None,
        error: str | None,
        now: float,
        mgr: Any,
    ) -> dict[str, Any]:
        if snap is None:
            return {"ok": False, "required": True, "error": error, "streams": []}
        streams: list[dict[str, Any]] = []
        supervised = {s.session_id for s in snap.sessions if s.supervised}
        for sid in list(self._first_supervised):
            if sid not in supervised:
                del self._first_supervised[sid]
        for health in snap.sessions:
            if not health.supervised or health.status != "running":
                continue
            session = mgr.get_session(health.session_id)
            if session is None:
                continue
            first_seen = self._first_supervised.setdefault(health.session_id, now)
            for symbol in session.symbols:
                streams.append(
                    self._stream(
                        health, session.exchange_id, symbol, session.timeframe, first_seen, now
                    )
                )
        return {"ok": all(s["ok"] for s in streams), "required": True, "streams": streams}

    def _stream(
        self,
        health: SessionHealth,
        exchange_id: str,
        symbol: str,
        timeframe: str,
        first_seen: float,
        now: float,
    ) -> dict[str, Any]:
        labels = {"exchange_id": exchange_id, "symbol": symbol, "timeframe": timeframe}
        message_ts = _gauge_value("hydra_feed_last_message_timestamp_seconds", labels)
        bar_ts = _gauge_value("hydra_feed_last_closed_bar_timestamp_seconds", labels)
        interval = _gauge_value("hydra_feed_bar_interval_seconds", labels)
        interval_s = interval if interval else float(timeframe_seconds(timeframe))
        message_age = now - (message_ts if message_ts is not None else first_seen)
        bar_age = now - (bar_ts if bar_ts is not None else first_seen)
        stale: list[str] = sorted(
            {reason for reasons in health.feed_stale_reasons.values() for reason in reasons}
        )
        if message_age > FEED_MESSAGE_MAX_AGE_S:
            stale.append("message_age")
        if bar_age > closed_bar_max_age(interval_s):
            stale.append("closed_bar_age")
        return {
            "session_id": health.session_id,
            "exchange_id": exchange_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "live": health.feed_live,
            "last_message_age_s": round(message_age, 1),
            "last_closed_bar_age_s": round(bar_age, 1),
            "stale_reasons": stale,
            "ok": health.feed_live and not stale,
        }

    @staticmethod
    def _check_risk(snap: HealthSnapshot | None, error: str | None) -> dict[str, Any]:
        if snap is None:
            return {"ok": False, "required": True, "error": error}
        return {"ok": snap.risk_available, "required": True}

    # ------------------------------------------------------------------
    # Gauges
    # ------------------------------------------------------------------

    def _publish_ready(self, result: Readiness) -> None:
        for name in self._checks:
            metrics.set_ready(name, bool(result.checks[name]["ok"]))
        current: set[SessionKey] = set()
        for item in result.checks.get("sessions", {}).get("sessions", []):
            key = (item["session_id"], item["strategy_id"], item["mode"])
            metrics.set_session_state(*key, desired=item["desired"], supervised=item["supervised"])
            current.add(key)
        if result.sessions_known:
            for key in self._published - current:
                metrics.forget_session(*key)
            self._published = current
