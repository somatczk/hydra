"""Sentiment data fetchers: Fear & Greed Index and social metrics."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_FEAR_GREED_URL = "https://api.alternative.me/fng/"


async def fetch_fear_greed(limit: int = 30) -> list[dict[str, Any]]:
    """Fetch Bitcoin Fear & Greed Index from alternative.me (free, no API key).

    Returns list of ``{"timestamp": "...", "value": int, "classification": str}``.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_FEAR_GREED_URL, params={"limit": limit, "format": "json"})
            resp.raise_for_status()
            data = resp.json().get("data", [])
            return [
                {
                    "timestamp": item["timestamp"],
                    "value": int(item["value"]),
                    "classification": item["value_classification"],
                }
                for item in data
            ]
    except Exception:
        logger.exception("Failed to fetch Fear & Greed Index")
        return []
