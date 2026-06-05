"""Sam's Club price parsing for the internal "Vivaldi" BFF payload.

Sam's product/pricing endpoints sit behind Akamai bot protection, which a
plain server-side request from a datacenter IP (Render) cannot clear — so the
app does not fetch these directly in the comparator. In production, Sam's costs
come from the operator pasting their own purchase history (Inventory → "Paste
Sam's purchases"), which carries the actual paid price and needs no BFF call.

:func:`parse_products_payload` and the thin :func:`search_products` caller
remain for local testing only (where the host IP isn't blocked); neither is
wired into the live comparator dispatch.
"""

from __future__ import annotations

import re

import httpx

from app.services.price_fetcher.models import FetchError, PriceResult

# The browser path (bookmarklet / Playwright helper) hits this same endpoint
# from the samsclub.com origin so it inherits the member session + Akamai
# clearance. Kept here as the single source of truth for the request shape.
PRODUCT_SEARCH_PATH = "/api/node/vivaldi/browse/v2/products/search"

# Sam's Club sometimes returns prices as strings ("$1.98", "1.98 each"). Strip
# currency markers and qualifiers before float coercion so the parse doesn't
# silently drop a legitimate price.
_PRICE_NOISE_RE = re.compile(r"[\$,]|usd|each|/\s*ea\.?|/\s*ct\b", re.IGNORECASE)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.samsclub.com/",
    "Origin": "https://www.samsclub.com",
    "X-Enable-Personalization": "false",
}

_PRODUCT_SEARCH = f"https://www.samsclub.com{PRODUCT_SEARCH_PATH}"
_BASE_URL = "https://www.samsclub.com"


def _coerce_price(val: object) -> float | None:
    """Coerce a price field to float, accepting both numerics and dirty strings.

    Sam's BFF responses occasionally include currency-formatted strings
    (``"$1.98"``, ``"1.98 each"``); raw ``float(val)`` would raise. Strip
    known noise tokens then attempt the conversion.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val > 0 else None
    if not isinstance(val, str):
        return None
    cleaned = _PRICE_NOISE_RE.sub("", val).strip()
    if not cleaned:
        return None
    try:
        f = float(cleaned)
        return f if f > 0 else None
    except ValueError:
        return None


def _parse_price(rec: dict) -> float | None:
    """Try multiple known price field paths across API versions."""
    # v2 structure
    catalog = rec.get("productCatalogData") or {}
    sale_info = catalog.get("salePriceAndStatus") or {}
    for key in ("onSalePrice", "salePrice", "price"):
        p = _coerce_price(sale_info.get(key))
        if p is not None:
            return p

    # flat price fields
    for key in ("sams_price", "finalPrice", "price", "listPrice"):
        p = _coerce_price(rec.get(key))
        if p is not None:
            return p

    # nested price object
    price_obj = rec.get("price") or {}
    if isinstance(price_obj, dict):
        for key in ("finalPrice", "salePrice", "price"):
            p = _coerce_price(price_obj.get(key))
            if p is not None:
                return p

    return None


def _parse_unit_size(rec: dict) -> str | None:
    """Pull a human-readable pack size from any of the size fields Sam's exposes.

    The BFF payload variously shows ``unitSize``, ``packSize``,
    ``displaySize``, or a free-text ``packType``. Surfacing this on
    PriceResult.unit_size lets the results table distinguish "Coke 12oz 24pk"
    from "Coke 16.9oz 24pk" when the API returns both for the same query.
    """
    catalog = rec.get("productCatalogData") or {}
    for src in (rec, catalog, catalog.get("productAttributes") or {}):
        if not isinstance(src, dict):
            continue
        for key in ("unitSize", "packSize", "displaySize", "packType", "size"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def parse_products_payload(
    data: dict,
    club_id: str,
    max_results: int = 6,
) -> list[PriceResult]:
    """Turn a raw Vivaldi products/search response into PriceResult rows.

    Used by the server-side ``search_products`` for local testing. It accepts
    the full decoded JSON payload — the records can live under
    ``payload.records`` or directly under ``payload`` depending on API version.
    """
    payload = data.get("payload") if isinstance(data, dict) else None
    if isinstance(payload, dict):
        records = payload.get("records", [])
    elif isinstance(payload, list):
        records = payload
    else:
        records = []

    results: list[PriceResult] = []
    for rec in records[:max_results]:
        if not isinstance(rec, dict):
            continue
        name = (
            rec.get("skuDisplayName") or rec.get("productName") or rec.get("name") or ""
        ).strip()
        if not name:
            continue

        price = _parse_price(rec)
        url_path = rec.get("skuPrimaryUrl") or rec.get("canonicalUrl") or ""
        url = f"{_BASE_URL}{url_path}" if url_path and url_path.startswith("/") else url_path

        # Skip phantom rows: no price AND no URL means we can't show or save
        # anything useful. The dispatcher applies the same is_empty() guard
        # downstream, but dropping here keeps the result list tighter.
        if price is None and not url:
            continue

        avail = (rec.get("availabilityStatus") or "").upper()
        in_stock = avail == "IN_STOCK" if avail else None

        results.append(
            PriceResult(
                vendor_key="sams_club",
                vendor_name="Sam's Club",
                vendor_type="local_wholesale",
                product_name=name,
                unit_price=price,
                unit_size=_parse_unit_size(rec),
                url=url or None,
                in_stock=in_stock,
                min_order="Membership required",
                notes=f"Member price – club #{club_id}",
                source="api",
                confidence="high" if price else "low",
            )
        )
    return results


def search_products(
    query: str,
    club_id: str,
    max_results: int = 6,
) -> list[PriceResult]:
    """Server-side BFF search. Not used by the live comparator (Akamai blocks
    datacenter IPs); kept for local testing and as the canonical request shape
    the browser-side capture path mirrors."""
    try:
        with httpx.Client(timeout=15, follow_redirects=True, verify=False) as client:
            r = client.get(
                _PRODUCT_SEARCH,
                params={
                    "searchTerm": query,
                    "clubId": club_id,
                    "pageSize": str(max_results),
                    "sortKey": "relevance",
                    "sortOrder": "1",
                    "offset": "0",
                    "json": "true",
                },
                headers=_HEADERS,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        raise FetchError(f"Sam's Club product search failed: {exc}") from exc

    return parse_products_payload(data, club_id, max_results=max_results)
