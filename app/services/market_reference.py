"""Market-reference pricing: what *other sellers* charge for similar products.

This is the "market" side of the comparator, deliberately separate from the
cost side (ProductSource rows that drive margins). Nothing here is ever written
as a cost — it's read-only context so the operator can sanity-check sell prices
against the wider market. Every fetcher is best-effort and defensive: a missing
API key, a rate-limit, or a bad response degrades to "no data", never an error
that breaks the page.

Sources:
  • UPCitemdb  — barcode → recorded price band + merchant offers (keyless trial
    at 100/day; paid key raises the cap).
  • Open Prices (Open Food Facts) — crowd-sourced real shelf prices by barcode.
  • BLS Average Price Data — regional/national average for a few tracked
    grocery categories (a coarse backdrop, combined with Open Prices).
  • eBay Browse — live listings for similar items (OAuth client-credentials).
    Flagged ``weak`` because eBay listings are noisy resale, not retail.
"""

from __future__ import annotations

import base64
import logging
import statistics
import time
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0


@dataclass
class MarketReference:
    """One market source's read-only price summary for a product."""

    source: str  # upcitemdb | open_prices | bls | ebay
    label: str
    low: float | None = None
    high: float | None = None
    avg: float | None = None
    sample_count: int | None = None
    url: str | None = None
    notes: str = ""
    weak: bool = False  # noisy/approximate source — UI tags it "weak sourcing"

    def has_data(self) -> bool:
        return self.avg is not None or self.low is not None or self.high is not None


def _clean_upc(upc: str | None) -> str:
    """Keep only the digits of a barcode (handles spaces/dashes from paste)."""
    return "".join(ch for ch in (upc or "") if ch.isdigit())


def search_upc(query: str) -> str | None:
    """Best-effort: resolve a product name to a barcode via UPCitemdb search.

    Powers the "fill missing UPCs" catalog backfill so the operator doesn't type
    barcodes by hand. Returns the top match's UPC/EAN (digits only) or None. It's
    a best guess — the operator can correct any mismatch on the product form. The
    keyless trial endpoint is rate-limited (100/day), so callers should cap how
    many they resolve per run.
    """
    if not query.strip():
        return None
    try:
        if settings.upcitemdb_api_key:
            url = "https://api.upcitemdb.com/prod/v1/search"
            headers = {"user_key": settings.upcitemdb_api_key, "key_type": "3scale"}
        else:
            url = "https://api.upcitemdb.com/prod/trial/search"
            headers = {}
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                url,
                params={"s": query.strip(), "match_mode": "0", "type": "product"},
                headers=headers,
            )
        if resp.status_code != 200:
            logger.info("UPCitemdb search returned %s for %r", resp.status_code, query)
            return None
        items = resp.json().get("items") or []
        if not items:
            return None
        code = str(items[0].get("upc") or items[0].get("ean") or "")
        return _clean_upc(code) or None
    except Exception:
        logger.warning("UPCitemdb search failed for %r", query, exc_info=True)
        return None


# ── UPCitemdb ──────────────────────────────────────────────────────────────────


def fetch_upcitemdb(upc: str) -> MarketReference | None:
    """Barcode → recorded retail price band. Uses the paid endpoint when a key is
    set, otherwise the keyless trial (100 lookups/day)."""
    code = _clean_upc(upc)
    if not code:
        return None
    try:
        if settings.upcitemdb_api_key:
            url = "https://api.upcitemdb.com/prod/v1/lookup"
            headers = {"user_key": settings.upcitemdb_api_key, "key_type": "3scale"}
        else:
            url = "https://api.upcitemdb.com/prod/trial/lookup"
            headers = {}
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url, params={"upc": code}, headers=headers)
        if resp.status_code != 200:
            logger.info("UPCitemdb returned %s for %s", resp.status_code, code)
            return None
        items = resp.json().get("items") or []
        if not items:
            return None
        item = items[0]
        low = item.get("lowest_recorded_price")
        high = item.get("highest_recorded_price")
        offers = item.get("offers") or []
        offer_prices = [
            o["price"]
            for o in offers
            if isinstance(o.get("price"), (int, float)) and o["price"] > 0
        ]
        avg = round(statistics.mean(offer_prices), 2) if offer_prices else None
        ref = MarketReference(
            source="upcitemdb",
            label="UPCitemdb (recorded retail)",
            low=float(low) if low else None,
            high=float(high) if high else None,
            avg=avg,
            sample_count=len(offer_prices) or None,
            notes="Recorded retail price band across merchants.",
        )
        return ref if ref.has_data() else None
    except Exception:
        logger.warning("UPCitemdb lookup failed for %s", code, exc_info=True)
        return None


# ── Open Prices (Open Food Facts) ──────────────────────────────────────────────


def fetch_open_prices(upc: str) -> MarketReference | None:
    """Crowd-sourced real shelf prices for a barcode (Open Food Facts, ODbL)."""
    code = _clean_upc(upc)
    if not code:
        return None
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                "https://prices.openfoodfacts.org/api/v1/prices",
                params={"product_code": code, "size": 50},
            )
        if resp.status_code != 200:
            return None
        rows = resp.json().get("items") or []
        prices = [
            r["price"] for r in rows if isinstance(r.get("price"), (int, float)) and r["price"] > 0
        ]
        if not prices:
            return None
        return MarketReference(
            source="open_prices",
            label="Open Prices (community shelf prices)",
            low=round(min(prices), 2),
            high=round(max(prices), 2),
            avg=round(statistics.mean(prices), 2),
            sample_count=len(prices),
            url=f"https://prices.openfoodfacts.org/products/{code}",
            notes="Real prices submitted by shoppers.",
        )
    except Exception:
        logger.warning("Open Prices lookup failed for %s", code, exc_info=True)
        return None


# ── BLS Average Price Data ─────────────────────────────────────────────────────

# Vending product category → BLS Average Price series ID (U.S. city average).
# Only a handful of categories map cleanly; everything else returns no backdrop.
_BLS_SERIES: dict[str, tuple[str, str]] = {
    "beverage_water": ("APU0000FN1101", "Carbonated drinks, per 2 liters"),
    "beverage_soda": ("APU0000FN1101", "Carbonated drinks, per 2 liters"),
    "snack_chips": ("APU0000718311", "Potato chips, per 16 oz"),
    "snack_healthy": ("APU0000718311", "Potato chips, per 16 oz"),
}


def fetch_bls_average(category: str | None) -> MarketReference | None:
    """National average price for the product's category, when BLS tracks one.

    Coarse by design — a category-level backdrop, not a SKU price. Combined with
    Open Prices in the UI under "regional/market average".
    """
    series = _BLS_SERIES.get((category or "").strip())
    if not series:
        return None
    series_id, desc = series
    try:
        payload: dict[str, object] = {"seriesid": [series_id], "latest": True}
        if settings.bls_api_key:
            payload["registrationkey"] = settings.bls_api_key
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                "https://api.bls.gov/publicAPI/v2/timeseries/data/",
                json=payload,
            )
        if resp.status_code != 200:
            return None
        body = resp.json()
        rows = (body.get("Results", {}).get("series") or [{}])[0].get("data") or []
        if not rows:
            return None
        value = float(rows[0]["value"])
        period = f"{rows[0].get('periodName', '')} {rows[0].get('year', '')}".strip()
        return MarketReference(
            source="bls",
            label="BLS national average",
            avg=round(value, 2),
            sample_count=1,
            notes=f"{desc} — U.S. city avg ({period}).",
        )
    except Exception:
        logger.warning("BLS lookup failed for category %s", category, exc_info=True)
        return None


# ── eBay Browse API ────────────────────────────────────────────────────────────

_ebay_token: dict[str, float | str] = {"value": "", "expires_at": 0.0}


def _ebay_token_value() -> str | None:
    """Fetch/cache an eBay application OAuth token (client-credentials grant)."""
    if not (settings.ebay_client_id and settings.ebay_client_secret):
        return None
    now = time.time()
    if _ebay_token["value"] and float(_ebay_token["expires_at"]) > now + 30:
        return str(_ebay_token["value"])
    try:
        creds = f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode()
        auth = base64.b64encode(creds).decode()
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                "https://api.ebay.com/identity/v1/oauth2/token",
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
            )
        if resp.status_code != 200:
            logger.info("eBay token request returned %s", resp.status_code)
            return None
        data = resp.json()
        _ebay_token["value"] = data["access_token"]
        _ebay_token["expires_at"] = now + float(data.get("expires_in", 7200))
        return str(_ebay_token["value"])
    except Exception:
        logger.warning("eBay token request failed", exc_info=True)
        return None


def fetch_ebay(query: str) -> MarketReference | None:
    """Live eBay listings for similar items. Weak (resale, noisy), reference only."""
    token = _ebay_token_value()
    if not token or not query.strip():
        return None
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                "https://api.ebay.com/buy/browse/v1/item_summary/search",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": query.strip(), "limit": 20},
            )
        if resp.status_code != 200:
            return None
        summaries = resp.json().get("itemSummaries") or []
        prices = []
        for s in summaries:
            val = (s.get("price") or {}).get("value")
            try:
                if val is not None and float(val) > 0:
                    prices.append(float(val))
            except (TypeError, ValueError):
                continue
        if not prices:
            return None
        return MarketReference(
            source="ebay",
            label="eBay listings",
            low=round(min(prices), 2),
            high=round(max(prices), 2),
            avg=round(statistics.mean(prices), 2),
            sample_count=len(prices),
            url=f"https://www.ebay.com/sch/i.html?_nkw={httpx.QueryParams({'q': query})['q']}",
            notes="Live resale listings — noisy, treat as a loose ceiling.",
            weak=True,
        )
    except Exception:
        logger.warning("eBay search failed for %s", query, exc_info=True)
        return None


# ── Orchestrator ───────────────────────────────────────────────────────────────


def gather_market_reference(
    *, query: str, upc: str | None = None, category: str | None = None
) -> list[MarketReference]:
    """Run every market source and return the ones that produced data.

    ``upc`` drives the barcode lookups (UPCitemdb, Open Prices); ``query`` drives
    eBay; ``category`` drives the BLS backdrop. Order is stable: barcode-keyed
    retail first, then averages, then the weak eBay ceiling last.
    """
    out: list[MarketReference | None] = []
    if upc:
        out.append(fetch_upcitemdb(upc))
        out.append(fetch_open_prices(upc))
    out.append(fetch_bls_average(category))
    out.append(fetch_ebay(query))
    return [r for r in out if r is not None and r.has_data()]
