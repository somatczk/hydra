"""Public probes: ``/livez`` (Docker healthcheck) and ``/readyz`` (alerting).

Neither body carries secrets, balances or positions.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

import hydra
from hydra.dashboard.health import HealthMonitor

router = APIRouter(tags=["health"])


def _monitor(request: Request) -> HealthMonitor | None:
    monitor = getattr(request.app.state, "health_monitor", None)
    return monitor if isinstance(monitor, HealthMonitor) else None


def _liveness(request: Request) -> JSONResponse:
    monitor = _monitor(request)
    alive = monitor is not None and monitor.alive()
    body: dict[str, Any] = {"status": "ok" if alive else "down", "version": hydra.__version__}
    return JSONResponse(body, status_code=200 if alive else 503)


@router.get("/livez")
async def livez(request: Request) -> JSONResponse:
    """200 once startup completed and the health monitor ran within three intervals."""
    return _liveness(request)


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """200 when every required check passes, else 503; the body names each check."""
    monitor = _monitor(request)
    if monitor is None:
        body = {"status": "not_ready", "version": hydra.__version__, "checks": {}}
        return JSONResponse(body, status_code=503)
    result = await monitor.readiness()
    return JSONResponse(result.body(), status_code=200 if result.ready else 503)
