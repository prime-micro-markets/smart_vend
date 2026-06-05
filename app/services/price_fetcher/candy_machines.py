"""CandyMachines price fetcher — HTML scraper.

When the site's card selectors fail (the ASP-era markup changes more often than
expected), the fetcher falls back to scraping every anchor that looks like a
product link. That fallback happens to surface *off-domain* links the site
itself promotes — boxncase.com, shoptheking.com, rdmsales.com, etc. — which are
legitimate vendor candidates we want the operator to see. Those off-domain
rows are routed to their own pseudo-vendor (``vendor_key="cm_ref_<host>"``)
so the comparator results template can group them as "Discovered via
CandyMachines" instead of mislabelling them as CandyMachines prices.

When the operator clicks **Save** on one of those rows, the comparator's
save-to-product flow uses ``vendor_name`` (the host) to find or create a
Supplier, so a single comparator run can both price-check a SKU *and* turn up
brand-new supplier candidates the team didn't know served their region.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.services.price_fetcher.models import FetchError, PriceResult

_SEARCH_URL = "https://www.candymachines.com/SearchResults.asp"
_BASE_URL = "https://www.candymachines.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.candymachines.com/",
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


def _resolve_url(href: str) -> str | None:
    """Normalize relative hrefs to absolute candymachines.com URLs; keep absolutes intact."""
    if not href:
        return None
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return f"{_BASE_URL}/{href.lstrip('/')}"


def _route_by_host(url: str | None) -> tuple[str, str, str, str]:
    """Pick (vendor_key, vendor_name, vendor_type, source) for a link based on host.

    On-domain hits → real CandyMachines results. Off-domain hits (boxncase.com,
    shoptheking.com, rdmsales.com, …) are vendor candidates we route to their
    own pseudo-vendor so the operator can see who promotes what, save them as
    new Suppliers, and potentially add them to AI Sourcing or Supplier Import
    later.
    """
    host = (urlparse(url).hostname or "").lower() if url else ""
    host = host.removeprefix("www.")
    if not host or host.endswith("candymachines.com"):
        return ("candy_machines", "CandyMachines", "online_vending", "scrape")
    # Slugify the host for a stable pseudo-vendor key.
    key_safe = re.sub(r"[^a-z0-9]+", "_", host).strip("_")
    return (f"cm_ref_{key_safe}", host, "online_vending", "cm_referral")


def search_products(
    query: str,
    account_email: str | None = None,
    max_results: int = 6,
) -> list[PriceResult]:
    try:
        with httpx.Client(timeout=10, follow_redirects=True, verify=False) as client:
            r = client.get(
                _SEARCH_URL,
                params={"Search": query},
                headers=_HEADERS,
            )
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        raise FetchError(f"CandyMachines search failed: {exc}") from exc

    soup = BeautifulSoup(html, "lxml")
    results: list[PriceResult] = []

    # CandyMachines uses older ASP layout — try table-based + div-based selectors
    selectors = [
        ".product-item",
        ".ProductItem",
        ".product",
        "td.product",
        ".item",
        "[class*='product']",
    ]
    cards = []
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            break

    # Fallback: scrape anchors that look like product links. The fallback also
    # surfaces off-domain candidates (vendors CandyMachines points its search
    # at) — see module docstring.
    if not cards:
        product_links = soup.select("a[href*='product'], a[href*='item'], a[href*='.htm']")
        for link in product_links[:max_results]:
            name = link.get_text(strip=True) or link.get("title", "")
            if not name or len(name) < 3:
                continue
            href = link.get("href", "")
            url = _resolve_url(href)
            # Try to find price near the link
            parent = link.parent
            price_text = ""
            for el in (parent, parent.parent if parent else None):
                if el:
                    price_el = el.select_one("[class*='price'], .price, span, td")
                    if price_el:
                        price_text = price_el.get_text(strip=True)
                        if "$" in price_text:
                            break
            vendor_key, vendor_name, vendor_type, source = _route_by_host(url)
            is_offdomain = vendor_key != "candy_machines"
            results.append(
                PriceResult(
                    vendor_key=vendor_key,
                    vendor_name=vendor_name,
                    vendor_type=vendor_type,
                    product_name=name,
                    unit_price=_parse_price(price_text),
                    url=url,
                    in_stock=True,
                    notes=(
                        "Found via CandyMachines search; off-domain link — verify supplier"
                        if is_offdomain
                        else "Bulk vending candy & snacks"
                    ),
                    source=source,
                    # Off-domain links carry no authoritative price/availability,
                    # so confidence stays low even when a $ shows up nearby.
                    confidence=(
                        "low" if is_offdomain else ("medium" if _parse_price(price_text) else "low")
                    ),
                )
            )
            if len(results) >= max_results:
                break
        return results

    for card in cards[:max_results]:
        name_el = card.select_one("a, h2, h3, h4, .name, .title")
        price_el = card.select_one(".price, [class*='price'], .amount, strong")
        link_el = card.select_one("a[href]")

        name = name_el.get_text(strip=True) if name_el else ""
        if not name:
            continue

        price_text = price_el.get_text(strip=True) if price_el else ""
        price = _parse_price(price_text)

        href = link_el["href"] if link_el else ""
        url = _resolve_url(href)

        vendor_key, vendor_name, vendor_type, source = _route_by_host(url)
        is_offdomain = vendor_key != "candy_machines"
        results.append(
            PriceResult(
                vendor_key=vendor_key,
                vendor_name=vendor_name,
                vendor_type=vendor_type,
                product_name=name,
                unit_price=price,
                url=url,
                in_stock=True,
                notes=(
                    "Found via CandyMachines search; off-domain link — verify supplier"
                    if is_offdomain
                    else "Bulk vending candy & snacks"
                ),
                source=source,
                confidence=("low" if is_offdomain else ("high" if price else "medium")),
            )
        )

    return results
