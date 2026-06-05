"""Thin wrapper around the Tavily search API."""

from __future__ import annotations

from typing import Any

from app.config import settings

_client: Any = None


def _get_client() -> Any:
    global _client
    if _client is not None:
        return _client
    if not settings.tavily_api_key:
        raise RuntimeError("TAVILY_API_KEY is not configured in .env")
    try:
        from tavily import TavilyClient  # type: ignore[import-untyped]

        _client = TavilyClient(api_key=settings.tavily_api_key)
    except ImportError as exc:
        raise RuntimeError("tavily-python not installed. Run: pip install tavily-python") from exc
    return _client


def search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Search the web via Tavily. Returns list of {title, url, content, score} dicts."""
    client = _get_client()
    # Bounded timeout so a stalled request can't hang the price-comparison job
    # (the tavily client passes this through to its underlying httpx call).
    response = client.search(query=query, max_results=max_results, timeout=8)
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "score": r.get("score", 0.0),
        }
        for r in response.get("results", [])
    ]
