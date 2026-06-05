"""DuckDuckGo search wrapper — free alternative to Tavily."""

from __future__ import annotations

from typing import Any


def search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    try:
        from ddgs import DDGS  # type: ignore[import-untyped]
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("ddgs not installed. Run: pip install ddgs") from exc
    results: list[dict[str, Any]] = []
    # An explicit timeout keeps a stalled connection (e.g. a local proxy/SSL
    # interception that never completes the TLS handshake) from hanging the
    # whole price-comparison job. Without it ddgs waits indefinitely.
    with DDGS(timeout=8) as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "content": r.get("body", ""),
                    "score": 0.0,
                }
            )
    return results
