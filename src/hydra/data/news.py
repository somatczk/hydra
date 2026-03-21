"""Crypto news aggregation from free APIs."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_COINGECKO_NEWS_URL = "https://api.coingecko.com/api/v3/news"
_CRYPTOCOMPARE_NEWS_URL = "https://min-api.cryptocompare.com/data/v2/news/?lang=EN"


async def fetch_crypto_news(limit: int = 20) -> list[dict[str, Any]]:
    """Fetch latest crypto news from CryptoCompare (free, no API key).

    Falls back to CoinGecko if CryptoCompare is unavailable.
    Returns list of ``{"title": str, "source": str, "url": str, "published_at": str}``.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Primary: CryptoCompare (free, no key required for news)
            resp = await client.get(_CRYPTOCOMPARE_NEWS_URL)
            resp.raise_for_status()
            data = resp.json().get("Data", [])
            results = []
            for item in data[:limit]:
                results.append(
                    {
                        "title": item.get("title", ""),
                        "source": item.get("source", ""),
                        "url": item.get("url", ""),
                        "published_at": item.get("published_on", ""),
                    }
                )
            return results
    except httpx.HTTPStatusError:
        logger.warning("CryptoCompare news API returned error, trying CoinGecko fallback")
    except Exception:
        logger.warning("CryptoCompare news fetch failed, trying CoinGecko fallback")

    # Fallback: CoinGecko
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_COINGECKO_NEWS_URL)
            resp.raise_for_status()
            data = resp.json().get("data", resp.json() if isinstance(resp.json(), list) else [])
            results = []
            for item in data[:limit]:
                results.append(
                    {
                        "title": item.get("title", ""),
                        "source": item.get("author", item.get("news_site", "")),
                        "url": item.get("url", ""),
                        "published_at": item.get("updated_at", item.get("created_at", "")),
                    }
                )
            return results
    except httpx.HTTPStatusError:
        logger.warning("CoinGecko news API returned error — endpoint may be deprecated")
    except Exception:
        logger.warning("CoinGecko news fetch failed")
    return []
