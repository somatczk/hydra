"""Prometheus metrics export (moved from api.py so the trader serves the same route).

Uses lazy import to avoid pulling ``prometheus_client`` at module level; the response
carries the OpenMetrics content type so Prometheus scrapes it without issues.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["observability"])


@router.get("/metrics")
async def metrics() -> Any:
    """Expose Prometheus metrics in OpenMetrics / text format."""
    try:
        import prometheus_client
        from starlette.responses import Response

        # Ensure metric collectors are registered before generating output
        from hydra.core.metrics import _ensure_initialized

        _ensure_initialized()

        return Response(
            content=prometheus_client.generate_latest(),
            media_type=prometheus_client.CONTENT_TYPE_LATEST,
        )
    except ImportError:
        return {"error": "prometheus_client not installed"}
