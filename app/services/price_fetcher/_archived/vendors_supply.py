"""Vendors Supply price fetcher — HTML scraper."""

from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from app.services.price_fetcher.models import FetchError, PriceResult

_SEARCH_URL = "https://www.vendorssupply.com/search"
_BASE_URL = "https://www.vendorssupply.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.vendorssupply.com/",
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


def search_products(
    query: str,
    account_email: str | None = None,
    max_results: int = 6,
) -> list[PriceResult]:
    try:
        with httpx.Client(timeout=20, follow_redirects=True, verify=False) as client:
            r = client.get(
                _SEARCH_URL,
                params={"q": query},
                headers=_HEADERS,
            )
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        raise FetchError(f"Vendors Supply search failed: {exc}") from exc

    soup = BeautifulSoup(html, "lxml")
    results: list[PriceResult] = []

    # Product grid selectors — try several patterns common on Shopify/Magento-style sites
    selectors = [
        ".product-item",
        ".product-card",
        ".grid-item",
        "[data-product]",
        ".product",
        "li.item",
    ]
    cards = []
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            break

    for card in cards[:max_results]:
        name_el = card.select_one("h2, h3, h4, .product-name, .product-title, .item-name, a[title]")
        price_el = card.select_one(".price, .product-price, .item-price, [class*='price'], .amount")
        link_el = card.select_one("a[href]")

        name = ""
        if name_el:
            name = name_el.get_text(strip=True) or name_el.get("title", "")
        if not name and link_el:
            name = link_el.get("title") or link_el.get_text(strip=True)
        if not name:
            continue

        price_text = price_el.get_text(strip=True) if price_el else ""
        price = _parse_price(price_text)

        href = link_el["href"] if link_el else ""
        url = f"{_BASE_URL}{href}" if href and href.startswith("/") else href or None

        results.append(
            PriceResult(
                vendor_key="vendors_supply",
                vendor_name="Vendors Supply",
                vendor_type="online_vending",
                product_name=name,
                unit_price=price,
                url=url,
                in_stock=True,
                notes="Vending-specific portions",
                source="scrape",
                confidence="high" if price else "medium",
            )
        )

    return results
