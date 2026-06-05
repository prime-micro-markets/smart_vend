"""WebstaurantStore price fetcher — parses search result HTML."""

from __future__ import annotations

import json
import re

import httpx
from bs4 import BeautifulSoup

from app.services.price_fetcher.models import FetchError, PriceResult

_SEARCH_URL = "https://www.webstaurantstore.com/search/"
_BASE_URL = "https://www.webstaurantstore.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.webstaurantstore.com/",
    "Upgrade-Insecure-Requests": "1",
}

_PRICE_RE = re.compile(r"\$[\d,]+\.?\d*")


def _parse_price(text: str) -> float | None:
    m = _PRICE_RE.search(text)
    if m:
        try:
            return float(m.group().replace("$", "").replace(",", ""))
        except ValueError:
            pass
    return None


def _items_from_next_data(html: str) -> list[dict]:
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
        # Path varies; try common locations
        for path in [
            ["props", "pageProps", "products"],
            ["props", "pageProps", "searchData", "products"],
            ["props", "pageProps", "initialData", "products"],
        ]:
            node = data
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, list) and node:
                return node
    except Exception:
        pass
    return []


def _items_from_json_ld(html: str) -> list[dict]:
    items = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    ):
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict) and obj.get("@type") == "ItemList":
                for el in obj.get("itemListElement", []):
                    if isinstance(el, dict) and el.get("item"):
                        items.append(el["item"])
            elif isinstance(obj, dict) and obj.get("@type") == "Product":
                items.append(obj)
        except Exception:
            pass
    return items


def _items_from_html(soup: BeautifulSoup) -> list[dict]:
    """Parse product cards from rendered HTML — multiple selector attempts."""
    raw: list[dict] = []

    # Strategy 1: data attributes on product wrappers
    for card in soup.select(
        "[data-testid='product-card'], .product-card, .product-box, [data-product-id]"
    ):
        name_el = card.select_one("[data-testid='product-name'], .product-title, h2, h3, .name")
        price_el = card.select_one(
            "[data-testid='product-price'], .product-price, .price, .sale-price"
        )
        link_el = card.select_one("a[href]")

        name = name_el.get_text(strip=True) if name_el else ""
        price_text = price_el.get_text(strip=True) if price_el else ""
        href = link_el["href"] if link_el else ""

        if name:
            raw.append({"name": name, "price_text": price_text, "href": href})

    # Strategy 2: any element with a price adjacent to a product name
    if not raw:
        for el in soup.select(".product, .item, article"):
            name_el = el.select_one("h2, h3, h4, .title, .name")
            price_el = el.select_one(".price, .cost, [class*='price']")
            link_el = el.select_one("a[href]")
            if name_el:
                raw.append(
                    {
                        "name": name_el.get_text(strip=True),
                        "price_text": price_el.get_text(strip=True) if price_el else "",
                        "href": link_el["href"] if link_el else "",
                    }
                )

    return raw


def search_products(
    query: str,
    account_email: str | None = None,
    max_results: int = 6,
) -> list[PriceResult]:
    # Firecrawl-first: resilient structured extraction. Falls through to the
    # BeautifulSoup path below if it yields nothing.
    from app.services.price_fetcher.firecrawl_extract import fetch_via_firecrawl

    fc = fetch_via_firecrawl(
        f"{_SEARCH_URL}?term={query}&type=product",
        query,
        "webstaurantstore",
        base_url=_BASE_URL,
        max_results=max_results,
        notes="B2B food service pricing",
    )
    if fc:
        return fc

    try:
        with httpx.Client(timeout=10, follow_redirects=True, verify=False) as client:
            r = client.get(
                _SEARCH_URL,
                params={"term": query, "type": "product"},
                headers=_HEADERS,
            )
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        raise FetchError(f"WebstaurantStore search failed: {exc}") from exc

    soup = BeautifulSoup(html, "lxml")
    results: list[PriceResult] = []

    # Try structured data first, fall back to HTML
    items = _items_from_next_data(html) or _items_from_json_ld(html)

    if items:
        for item in items[:max_results]:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            price: float | None = None
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price_val = offers.get("price") or item.get("price")
            if price_val:
                try:
                    price = float(str(price_val).replace("$", "").replace(",", ""))
                except ValueError:
                    pass
            url = item.get("url") or item.get("@id") or ""
            if url and not url.startswith("http"):
                url = f"{_BASE_URL}{url}"
            results.append(
                PriceResult(
                    vendor_key="webstaurantstore",
                    vendor_name="WebstaurantStore",
                    vendor_type="online_wholesale",
                    product_name=name,
                    unit_price=price,
                    url=url or None,
                    in_stock=True,
                    min_order="Typically $99 minimum",
                    notes="B2B food service pricing",
                    source="scrape",
                    confidence="high" if price else "medium",
                )
            )
    else:
        # HTML fallback
        raw_cards = _items_from_html(soup)
        for card in raw_cards[:max_results]:
            name = card["name"]
            price = _parse_price(card.get("price_text", ""))
            href = card.get("href", "")
            url = f"{_BASE_URL}{href}" if href and href.startswith("/") else href or None
            results.append(
                PriceResult(
                    vendor_key="webstaurantstore",
                    vendor_name="WebstaurantStore",
                    vendor_type="online_wholesale",
                    product_name=name,
                    unit_price=price,
                    url=url,
                    in_stock=True,
                    min_order="Typically $99 minimum",
                    notes="B2B food service pricing",
                    source="scrape",
                    confidence="medium" if price else "low",
                )
            )

    return results
