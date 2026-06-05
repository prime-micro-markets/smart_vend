"""Unified web search dispatcher with a resilient fallback chain.

Preferred provider is tried first; on any failure (missing key, network
error, rate limit) it logs a warning and falls back to DuckDuckGo so the
caller always gets results instead of an exception.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _duckduckgo(query: str, max_results: int) -> list[dict[str, Any]]:
    from app.services import duckduckgo_search

    return duckduckgo_search.search(query, max_results)


def _tavily(query: str, max_results: int) -> list[dict[str, Any]]:
    from app.services import tavily

    return tavily.search(query, max_results)


def search(query: str, max_results: int = 5, provider: str = "duckduckgo") -> list[dict[str, Any]]:
    """Run a web search, falling back to DuckDuckGo if the preferred provider fails.

    Returns a list of {title, url, content, score} dicts. Never raises for a
    provider failure — returns [] only if every provider fails.
    """
    # Hard-cap query length. Tavily rejects >400 chars and DuckDuckGo truncates
    # silently; either way an overly long query is a bug (typically a SQLAlchemy
    # Query object stringified by accident — keep this guard even after the
    # known shadow bug is patched).
    if len(query) > 380:
        logger.warning(
            "Search query truncated from %d to 380 chars; head=%r", len(query), query[:120]
        )
        query = query[:380]

    if provider == "tavily":
        try:
            results = _tavily(query, max_results)
            if results:
                return results
            logger.warning("Tavily returned no results for %r; falling back to DuckDuckGo", query)
        except Exception:
            logger.warning(
                "Tavily search failed for %r; falling back to DuckDuckGo",
                query,
                exc_info=True,
            )

    try:
        return _duckduckgo(query, max_results)
    except Exception:
        logger.exception("DuckDuckGo search failed for %r; returning no results", query)
        return []
